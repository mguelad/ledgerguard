import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from modules.accounts import maintenance
from modules.accounts.models import Membership, Organization, TenantDirectory
from modules.accounts.tenancy import audit, tenant_scope
from modules.findings import notifications
from modules.findings.models import AlertRoute, EmailDelivery, Finding, FindingEvent, Outbox
from modules.ingestion.service import enqueue
from modules.reporting import service as reports
from modules.reporting.models import Report
from tests.test_database import organization as organization
from tests.test_database import store as store
from workers import runtime

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def case(store):
    with tenant_scope(store.organization_id):
        Organization.objects.filter(id=store.organization_id).update(alerts_enabled=True)
        store.alerts_enabled = True
        store.save()
        user = get_user_model().objects.create_user(username="operator", email="operator@example.com")
        member = Membership.objects.create(organization_id=store.organization_id, user=user, role="owner")
        route = AlertRoute.objects.create(
            organization_id=store.organization_id, email=user.email, verified_at=timezone.now()
        )
        finding = Finding.objects.create(
            organization_id=store.organization_id,
            store=store,
            mode="test",
            finding_key="a" * 64,
            rule_code="PI-001",
            rule_version="1.0",
            policy_version=1,
            severity="high",
            state="open",
            identities=["payment:pi_one"],
            risk_amount_minor=12995,
            currency="EUR",
            exponent=2,
            match_confidence=100,
            eligible_at=timezone.now() - timedelta(hours=1),
            evidence_hash="b" * 64,
        )
        event = FindingEvent.objects.create(
            organization_id=store.organization_id,
            finding=finding,
            event_type="opened",
            actor_id="system",
            previous_state="provisional",
            new_state="open",
        )
        return SimpleNamespace(
            store=store, org=store.organization_id, member=member, route=route, finding=finding, event=event
        )


def test_delivery_deduplication_and_terminal_feedback(case, settings, monkeypatch):
    settings.SES_FROM_EMAIL = "alerts@example.com"
    settings.SES_CONFIGURATION_SET = "ledgerguard"
    sends = []
    monkeypatch.setattr(
        notifications.boto3,
        "client",
        lambda *a, **k: SimpleNamespace(send_email=lambda **v: sends.append(v) or {"MessageId": "provider-message"}),
    )
    with tenant_scope(case.org):
        notifications.prepare(case.event.id)
        notifications.prepare(case.event.id)
        row = EmailDelivery.objects.get()
        assert Outbox.objects.filter(task_type="deliver_email").count() == 1
        notifications.deliver(row.id)
        notifications.deliver(row.id)
        assert len(sends) == 1 and sends[0]["ConfigurationSetName"] == "ledgerguard"
        assert "12995" not in sends[0]["Message"]["Body"]["Text"]["Data"]
    value = {
        "eventType": "Bounce",
        "mail": {"messageId": "provider-message", "tags": {"organization": [str(case.org)], "delivery": [str(row.id)]}},
    }
    notifications.feedback(value)
    notifications.feedback(value | {"eventType": "Delivery"})
    with tenant_scope(case.org):
        row.refresh_from_db()
        case.route.refresh_from_db()
        assert row.status == "bounced" and not case.route.active


def test_security_notice_is_transactional_owner_only_and_ignores_finding_alert_switch(case, settings, monkeypatch):
    settings.SES_FROM_EMAIL = "alerts@example.com"
    settings.SES_CONFIGURATION_SET = "ledgerguard"
    sends = []
    monkeypatch.setattr(
        notifications.boto3,
        "client",
        lambda *a, **k: SimpleNamespace(send_email=lambda **v: sends.append(v) or {"MessageId": "security-message"}),
    )
    with tenant_scope(case.org):
        viewer = get_user_model().objects.create_user(username="viewer", email="viewer@example.com")
        Membership.objects.create(organization_id=case.org, user=viewer, role="viewer")
        AlertRoute.objects.create(organization_id=case.org, email=viewer.email, verified_at=timezone.now())
        Organization.objects.filter(id=case.org).update(alerts_enabled=False)
        event = audit(case.org, "owner", "member.changed", case.member.id, {"role": "analyst"})
        assert Outbox.objects.filter(task_type="security_notify", resource_id=event.id).count() == 1
        notifications.security(event.id)
        notifications.security(event.id)
        delivery = EmailDelivery.objects.get(security_event=event)
        assert delivery.route == case.route
        assert EmailDelivery.objects.filter(security_event=event).count() == 1
        assert Outbox.objects.filter(task_type="deliver_email", resource_id=delivery.id).count() == 1
        notifications.deliver(delivery.id)
        notifications.deliver(delivery.id)
        assert len(sends) == 1
        assert sends[0]["Destination"]["ToAddresses"] == [case.route.email]
        assert sends[0]["Message"]["Subject"]["Data"] == "Member access changed"
        assert "analyst" not in sends[0]["Message"]["Body"]["Text"]["Data"]


def test_deletion_notice_can_finish_after_intake_is_disabled(case, settings, monkeypatch):
    settings.SES_FROM_EMAIL = "alerts@example.com"
    sends = []
    monkeypatch.setattr(
        notifications.boto3,
        "client",
        lambda *a, **k: SimpleNamespace(send_email=lambda **v: sends.append(v) or {"MessageId": "deletion-message"}),
    )
    with tenant_scope(case.org):
        event = audit(case.org, "owner", "organization.deletion_requested", case.org)
        Organization.objects.filter(id=case.org).update(active=False)
        notice = Outbox.objects.get(task_type="security_notify", resource_id=event.id)
        notice_message = runtime.message(notice)
    assert runtime.run_message(notice_message)
    with tenant_scope(case.org):
        delivery = EmailDelivery.objects.get(security_event=event)
        delivery_work = Outbox.objects.get(task_type="deliver_email", resource_id=delivery.id)
        delivery_message = runtime.message(delivery_work)
    assert runtime.run_message(delivery_message)
    assert len(sends) == 1
    assert sends[0]["Message"]["Subject"]["Data"] == "Organization deletion requested"


@pytest.mark.parametrize("change", ["revoked", "merchant", "kill_org", "kill_store", "suppressed", "inactive_route"])
def test_queued_delivery_rechecks_access(case, change, monkeypatch):
    def unexpected(*a, **k):
        raise AssertionError("Cancelled delivery contacted provider")

    monkeypatch.setattr(notifications.boto3, "client", unexpected)
    with tenant_scope(case.org):
        notifications.prepare(case.event.id)
        row = EmailDelivery.objects.get()
        if change == "revoked":
            Membership.objects.filter(id=case.member.id).update(active=False)
        if change == "merchant":
            Membership.objects.filter(id=case.member.id).update(role="merchant", store_id=case.store.id)
        if change == "kill_org":
            Organization.objects.filter(id=case.org).update(alerts_enabled=False)
        if change == "kill_store":
            type(case.store).objects.filter(id=case.store.id).update(alerts_enabled=False)
        if change == "suppressed":
            Finding.objects.filter(id=case.finding.id).update(state="suppressed")
        if change == "inactive_route":
            AlertRoute.objects.filter(id=case.route.id).update(active=False)
        notifications.deliver(row.id)
        row.refresh_from_db()
        assert row.status == "cancelled"


def test_rate_limit_creates_one_tracked_digest(case, settings):
    settings.SES_FROM_EMAIL = ""
    with tenant_scope(case.org):
        for _ in range(23):
            notifications.prepare(case.event.id)
        intent = Outbox.objects.get(task_type="notify_digest")
        assert intent.available_at > timezone.now() + timedelta(minutes=59)
        notifications.digest(case.org, intent.dedupe_key)
        notifications.digest(case.org, intent.dedupe_key)
        row = EmailDelivery.objects.get(event__isnull=True)
        notifications.deliver(row.id)
        row.refresh_from_db()
        assert row.status == "local" and row.digest_key


@pytest.mark.parametrize("format", ["csv", "html", "pdf"])
def test_report_roundtrip_expiry(case, settings, tmp_path, format):
    settings.REPORT_BUCKET = ""
    settings.BASE_DIR = tmp_path
    with tenant_scope(case.org):
        report = Report.objects.create(
            organization_id=case.org,
            store=case.store,
            requested_by=case.member.user_id,
            format=format,
            expires_at=timezone.now() + timedelta(days=1),
        )
        reports.generate(report.id)
        report.refresh_from_db()
        content, kind = reports.download(report)
        assert kind == format and len(content) > 100 and len(report.content_sha256) == 64
        reports.generate(report.id)
        report.expires_at = timezone.now() - timedelta(seconds=1)
        with pytest.raises(Exception, match="REPORT_UNAVAILABLE"):
            reports.download(report)


def test_s3_report_encrypted_and_url_bounded(case, settings, monkeypatch):
    settings.REPORT_BUCKET = "private-reports"
    settings.KMS_KEY_ID = "key"
    requests = []
    client = SimpleNamespace(
        put_object=lambda **v: requests.append(v),
        generate_presigned_url=lambda *a, **v: requests.append(v) or "https://download.example.com/short",
    )
    monkeypatch.setattr(reports.boto3, "client", lambda *a, **k: client)
    with tenant_scope(case.org):
        report = Report.objects.create(
            organization_id=case.org,
            requested_by=case.member.user_id,
            format="csv",
            expires_at=timezone.now() + timedelta(days=1),
        )
        reports.generate(report.id)
        report.refresh_from_db()
        assert reports.download(report).endswith("/short")
        assert requests[0]["ServerSideEncryption"] == "aws:kms" and requests[1]["ExpiresIn"] == 60


@contextmanager
def maintenance_role():
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute("SET ROLE ledgerguard_maintenance")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("SET ROLE ledgerguard_app")


def test_verified_erasure_receipt(case, settings, tmp_path, monkeypatch):
    settings.REPORT_BUCKET = ""
    settings.BASE_DIR = tmp_path
    settings.DELETION_LEDGER_BUCKET = "ledger"
    settings.KMS_KEY_ID = "key"
    sent = []
    monkeypatch.setattr(
        maintenance.boto3, "client", lambda *a, **k: SimpleNamespace(put_object=lambda **v: sent.append(v))
    )
    directory = tmp_path / "var" / "reports" / str(case.org)
    directory.mkdir(parents=True)
    (directory / "one.csv").write_text("synthetic")
    with pytest.raises(PermissionError):
        maintenance.erase_tenant(case.org)
    with maintenance_role():
        with pytest.raises(ValueError, match="not been requested"):
            maintenance.erase_tenant(case.org)
    with tenant_scope(case.org):
        Organization.objects.filter(id=case.org).update(active=False, deleted_at=timezone.now())
    with maintenance_role():
        receipt = maintenance.erase_tenant(case.org)
        with tenant_scope(case.org):
            assert not Organization.objects.exists() and not Finding.objects.exists()
        assert TenantDirectory.objects.get(id=case.org).erasure_digest == receipt
    assert not directory.exists() and json.loads(sent[0]["Body"])["digest"] == receipt
    assert "operator@example.com" not in str(sent)


def test_retention_preserves_open_case(case, settings, tmp_path):
    settings.BASE_DIR = tmp_path
    settings.REPORT_BUCKET = ""
    key = f"reports/{case.org}/expired.csv"
    path = tmp_path / "var" / key
    path.parent.mkdir(parents=True)
    path.write_text("synthetic")
    with tenant_scope(case.org):
        Report.objects.create(
            organization_id=case.org,
            requested_by=case.member.user_id,
            format="csv",
            expires_at=timezone.now() - timedelta(seconds=1),
            object_key=key,
        )
    with maintenance_role():
        maintenance.retain(case.org)
    with tenant_scope(case.org):
        assert Finding.objects.count() == 1 and not Report.objects.exists()
    assert not path.exists()


def test_failed_report_visible_after_retry_budget(case, monkeypatch):
    with tenant_scope(case.org):
        report = Report.objects.create(
            organization_id=case.org,
            requested_by=case.member.user_id,
            format="csv",
            expires_at=timezone.now() + timedelta(days=1),
        )
        work = enqueue(case.org, "report", report.id, "failed-report")
        work.attempts = 7
        work.save()
        value = runtime.message(work)

    def fail(*a):
        raise RuntimeError("internal details never logged")

    monkeypatch.setattr(runtime, "handle", fail)
    assert not runtime.run_message(value)
    with tenant_scope(case.org):
        report.refresh_from_db()
        work.refresh_from_db()
        assert report.status == "failed" and work.status == "dead"
