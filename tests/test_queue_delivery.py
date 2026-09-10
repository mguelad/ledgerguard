import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from modules.accounts.tenancy import tenant_scope
from modules.connectors.models import Installation
from modules.connectors.stripe import ConnectorFailure
from modules.findings.models import Outbox
from modules.ingestion.models import Cursor, Scan
from modules.ingestion.service import enqueue
from tests.test_database import organization as organization
from tests.test_database import store as store
from workers import runtime

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


def test_dispatch_routes_references_and_reclaims_expired_leases(store, settings, monkeypatch):
    settings.QUEUE_URLS = {
        kind: "https://sqs.eu-west-1.amazonaws.com/123/" + kind
        for kind in ["notification", "reconciliation", "reporting", "repair", "ingestion"]
    }
    sqs = Mock()
    monkeypatch.setattr(runtime.boto3, "client", lambda *a, **k: sqs)
    with tenant_scope(store.organization_id):
        for kind in ["reconcile_store", "report", "notify", "stripe_poll", "stripe_receipt"]:
            enqueue(store.organization_id, kind, store.id, "queue-test-" + kind)
    assert runtime.dispatch() == 5 and sqs.send_message.call_count == 5
    for call in sqs.send_message.call_args_list:
        body = json.loads(call.kwargs["MessageBody"])
        assert set(body) == {
            "schema_version",
            "message_id",
            "task_type",
            "organization_id",
            "resource_id",
            "attempt_context",
            "traceparent",
        }
        assert call.kwargs["QueueUrl"] == runtime.queue_for(body["task_type"])
    assert runtime.dispatch() == 0
    with tenant_scope(store.organization_id):
        Outbox.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
    assert runtime.dispatch() == 5


def test_local_consumer_retries_and_deduplicates_completed_work(store, monkeypatch):
    handle = Mock()
    monkeypatch.setattr(runtime, "handle", handle)
    with tenant_scope(store.organization_id):
        enqueue(store.organization_id, "reconcile_store", store.id, "local-one")
        row = Outbox.objects.get(dedupe_key="local-one")
        envelope = runtime.message(row)
    assert runtime.local_once() == 1 and handle.call_count == 1
    assert runtime.run_message(envelope) is True and handle.call_count == 1
    assert runtime.local_once() == 0


def test_scheduler_deduplicates_each_time_bucket(store, monkeypatch):
    monkeypatch.setattr(
        runtime.timezone, "now", lambda: timezone.datetime(2026, 9, 7, 12, tzinfo=timezone.get_default_timezone())
    )
    runtime.schedule()
    runtime.schedule()
    with tenant_scope(store.organization_id):
        assert Outbox.objects.filter(task_type="reconcile_store").count() == 1


def test_feedback_consumer_preserves_poison_message_for_dlq(settings, monkeypatch):
    from modules.findings import notifications

    settings.SES_FEEDBACK_QUEUE_URL = "https://sqs.eu-west-1.amazonaws.com/123/feedback"
    sqs = Mock()
    sqs.receive_message.return_value = {
        "Messages": [
            {"Body": '{"eventType":"Delivery"}', "ReceiptHandle": "valid"},
            {"Body": "malformed", "ReceiptHandle": "poison"},
        ]
    }
    feedback = Mock()
    monkeypatch.setattr(notifications, "feedback", feedback)
    runtime.consume_feedback(sqs)
    assert feedback.call_count == 1
    sqs.delete_message.assert_called_once_with(QueueUrl=settings.SES_FEEDBACK_QUEUE_URL, ReceiptHandle="valid")


@pytest.mark.parametrize("permanent", [False, True])
def test_dead_connector_work_invalidates_coverage_and_closes_scan(store, monkeypatch, permanent):
    monkeypatch.setattr(runtime, "refresh_access_token", Mock())
    with tenant_scope(store.organization_id):
        connector = Installation.objects.create(
            organization_id=store.organization_id,
            kind="stripe",
            mode="test",
            account_id="acct_dead_work",
            credential_ciphertext={"ciphertext": "opaque-fixture"},
        )
        Cursor.objects.create(
            organization_id=store.organization_id,
            connector=connector,
            object_class="stripe_payments",
            covered_from=timezone.now() - timedelta(days=1),
            covered_through=timezone.now(),
            observed_at=timezone.now(),
            complete=True,
        )
        scan = Scan.objects.create(
            organization_id=store.organization_id,
            connector=connector,
            window_from=timezone.now() - timedelta(days=1),
            window_through=timezone.now(),
        )
        row = enqueue(store.organization_id, "stripe_scan_page", scan.id, f"dead-scan-{permanent}")
        if not permanent:
            row.attempts = 7
            row.save(update_fields=["attempts"])
        envelope = runtime.message(row)
    error = ConnectorFailure("STRIPE_AUTHORIZATION_REVOKED", permanent=permanent)
    monkeypatch.setattr(runtime, "handle", Mock(side_effect=error))
    assert not runtime.run_message(envelope)
    with tenant_scope(store.organization_id):
        row.refresh_from_db()
        connector.refresh_from_db()
        scan.refresh_from_db()
        assert row.status == "dead"
        assert not Cursor.objects.get(connector=connector).complete
        assert scan.status == "failed"
        assert connector.status == ("suspended" if permanent else "active")
        assert bool(connector.credential_ciphertext) == (not permanent)
