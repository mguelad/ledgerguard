import time
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone

from apps.control_plane.errors import Problem
from modules.accounts.models import AuditLog, Membership, Organization, TenantDirectory
from modules.accounts.tenancy import require_database_role, tenant_scope
from modules.connectors.models import Installation, ResourceLocator, Store
from modules.findings.models import AlertRoute, EmailDelivery, Finding, FindingEvent, Outbox
from modules.findings.service import reconcile
from modules.ingestion.models import Cursor, Observation, Projection
from modules.ingestion.service import accept_woo_batch, enqueue, upsert
from modules.ingestion.validation import utc
from tests.test_core import snapshot
from workers import runtime

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def organization():
    org = uuid4()
    with tenant_scope(org):
        TenantDirectory.objects.create(id=org)
        Organization.objects.create(id=org, organization_id=org, name="Northwind")
    return org


@pytest.fixture
def store(organization):
    with tenant_scope(organization):
        row = Store.objects.create(
            organization_id=organization, name="Northwind", hostname="northwind.example", mode="test"
        )
        ResourceLocator.objects.create(id=row.id, organization_id=organization, kind="store")
        return row


@pytest.fixture
def connector(store):
    with tenant_scope(store.organization_id):
        return Installation.objects.create(organization_id=store.organization_id, store=store, kind="woo", mode="test")


def facts(now=None):
    date = utc((now or timezone.now()) - timedelta(hours=1))
    return {
        "status": "processing",
        "amount_minor": 12995,
        "currency": "EUR",
        "exponent": 2,
        "created_at_source": date,
        "refunded_minor": 0,
        "method": "stripe",
        "transaction_id": "pi_one",
        "pi_id": "pi_one",
        "charge_id": "",
        "session_id": "",
        "paid_at": date,
        "refund_ids": [],
    }


def envelope(connector, **kwargs):
    now = timezone.now()
    data = {
        "schema_version": "1.0",
        "installation_id": str(connector.id),
        "request_id": str(uuid4()),
        "sent_at": utc(now),
        "scan_id": str(uuid4()),
        "page": 1,
        "final_page": True,
        "covered_from": utc(now - timedelta(days=35)),
        "covered_through": utc(now - timedelta(minutes=2)),
        "records": [],
    }
    return data | kwargs


def test_runtime_is_neither_owner_nor_bypass():
    require_database_role("ledgerguard_app")
    with pytest.raises(PermissionError, match="ledgerguard_maintenance"):
        require_database_role("ledgerguard_maintenance")
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        assert cursor.fetchone() == (False, False)
        cursor.execute("SELECT count(*) FROM pg_tables WHERE tableowner=current_user AND schemaname='public'")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='findings_finding'")
        assert cursor.fetchone() == (True, True)


def test_unscoped_reads_and_cross_tenant_writes_fail_closed(organization):
    assert Organization.objects.count() == 0
    with tenant_scope(uuid4()):
        assert not Organization.objects.filter(id=organization).exists()
        with pytest.raises(DatabaseError), transaction.atomic():
            Store.objects.create(organization_id=organization, name="Wrong", hostname="wrong.example", mode="test")
    with tenant_scope(organization):
        assert Organization.objects.get().name == "Northwind"
    assert Organization.objects.count() == 0


def test_tenant_context_cannot_change_inside_transaction(organization):
    with tenant_scope(organization):
        with pytest.raises(Problem, match="TENANT_CONTEXT_CONFLICT"), tenant_scope(uuid4()):
            pass
        assert Organization.objects.count() == 1


def test_composite_foreign_key_prevents_cross_tenant_parent(store):
    foreign_org = uuid4()
    with tenant_scope(foreign_org):
        with pytest.raises(DatabaseError), transaction.atomic():
            Installation.objects.create(organization_id=foreign_org, store=store, kind="woo", mode="test")
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_security_delivery_cannot_reference_another_tenants_audit_event(organization):
    with tenant_scope(organization):
        event = AuditLog.objects.create(
            organization_id=organization,
            actor_id="operator",
            action="member.changed",
            resource_id="1",
        )
    foreign_org = uuid4()
    user = get_user_model().objects.create_user(username="foreign-owner", email="foreign-owner@example.com")
    with tenant_scope(foreign_org):
        TenantDirectory.objects.create(id=foreign_org)
        Organization.objects.create(id=foreign_org, organization_id=foreign_org, name="Foreign")
        Membership.objects.create(organization_id=foreign_org, user=user, role="owner")
        route = AlertRoute.objects.create(
            organization_id=foreign_org,
            email=user.email,
            verified_at=timezone.now(),
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            EmailDelivery.objects.create(
                organization_id=foreign_org,
                security_event=event,
                route=route,
            )
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_audit_cannot_be_updated_or_deleted(organization):
    with tenant_scope(organization):
        row = AuditLog.objects.create(
            organization_id=organization, actor_id="operator", action="review", resource_id="1"
        )
        for operation in [
            lambda: AuditLog.objects.filter(id=row.id).update(action="rewritten"),
            lambda: AuditLog.objects.filter(id=row.id).delete(),
        ]:
            with pytest.raises(DatabaseError), transaction.atomic():
                operation()
        assert AuditLog.objects.get().action == "review"


def test_projection_duplicate_and_late_delivery(connector):
    now = timezone.now()
    with tenant_scope(connector.organization_id):
        row, status = upsert(connector, "order", "1", now, facts(now))
        assert status == "accepted"
        _, status = upsert(connector, "order", "1", now, facts(now))
        assert status == "duplicate" and Observation.objects.count() == 1
        upsert(connector, "order", "1", now - timedelta(hours=1), facts(now) | {"status": "pending"})
        row.refresh_from_db()
        assert row.status == "processing" and Observation.objects.count() == 2
        with pytest.raises(Problem, match="SOURCE_REVISION_CONFLICT"), transaction.atomic():
            upsert(connector, "order", "1", now, facts(now) | {"status": "pending"})


def test_projection_and_observation_roll_back_together(connector):
    with tenant_scope(connector.organization_id):
        with pytest.raises(RuntimeError), transaction.atomic():
            upsert(connector, "order", "1", timezone.now(), facts())
            raise RuntimeError("interrupted")
        assert not Observation.objects.exists() and not Projection.objects.exists()


def test_scan_requires_all_contiguous_pages_and_supports_resigning(connector):
    value = envelope(connector, final_page=False)
    with tenant_scope(connector.organization_id):
        assert not accept_woo_batch(connector, value)["coverage_advanced"]
        with pytest.raises(Problem, match="SCAN_SEQUENCE_CONFLICT"):
            accept_woo_batch(connector, value | {"page": 3, "request_id": str(uuid4()), "final_page": True})
        final = value | {"page": 2, "request_id": str(uuid4()), "final_page": True}
        assert accept_woo_batch(connector, final)["coverage_advanced"]
        cursor = Cursor.objects.get(connector=connector, object_class="woo_orders")
        revision = cursor.updated_at
        retry = final | {"request_id": str(uuid4()), "sent_at": utc(timezone.now())}
        assert accept_woo_batch(connector, retry)["request_id"] == retry["request_id"]
        cursor.refresh_from_db()
        assert cursor.updated_at == revision


def test_gap_and_partial_rejection_do_not_claim_coverage(connector):
    with tenant_scope(connector.organization_id):
        initial = envelope(connector, covered_through=utc(timezone.now() - timedelta(hours=1)))
        assert accept_woo_batch(connector, initial)["coverage_advanced"]
        gap = envelope(connector, covered_from=utc(timezone.now() - timedelta(minutes=30)))
        assert not accept_woo_batch(connector, gap)["coverage_advanced"]
        assert not Cursor.objects.get(connector=connector, object_class="woo_orders").complete
        invalid = {
            "kind": "order",
            "source_id": "1",
            "revision": utc(timezone.now()),
            "amount": "1e10",
            "currency": "EUR",
        }
        result = accept_woo_batch(connector, envelope(connector, records=[invalid]))
        assert result["results"][0]["status"] == "rejected"
        assert not result["coverage_advanced"]


def test_worker_checks_authoritative_reference_and_preserves_dlq(connector, monkeypatch):
    org = connector.organization_id
    with tenant_scope(org):
        row = enqueue(org, "stripe_poll", connector.id, "test-worker")
    mismatch = runtime.message(row) | {"resource_id": str(uuid4())}
    assert not runtime.run_message(mismatch)
    with tenant_scope(org):
        row.refresh_from_db()
        assert row.attempts == 0
        Outbox.objects.filter(id=row.id).update(status="dead")
    assert not runtime.run_message(runtime.message(row))


def test_worker_commits_effect_and_completion_once(connector, monkeypatch):
    org = connector.organization_id
    with tenant_scope(org):
        row = enqueue(org, "stripe_poll", connector.id, "test-once")
    effects = []
    monkeypatch.setattr(runtime, "handle", lambda row: effects.append(row.id))
    assert runtime.run_message(runtime.message(row))
    assert runtime.run_message(runtime.message(row))
    assert effects == [row.id]


def test_finding_needs_two_clean_runs_and_reopens(store, monkeypatch):
    org = store.organization_id
    source = snapshot(orders=())
    current = replace(
        source,
        organization_id=str(org),
        store_id=str(store.id),
        payments=(replace(source.payments[0], store_hint=str(store.id)),),
    )
    monkeypatch.setattr("modules.findings.service.build_snapshot", lambda store, now: current)
    with tenant_scope(org):
        reconcile(store.id, current.now)
        finding = Finding.objects.get(rule_code="PI-002")
        assert finding.state == "open"
        first_key = finding.finding_key
        current = replace(current, payments=())
        reconcile(store.id, current.now + timedelta(minutes=1))
        finding.refresh_from_db()
        assert finding.state == "open"
        reconcile(store.id, current.now + timedelta(minutes=6))
        finding.refresh_from_db()
        assert finding.state == "resolved"
        current = replace(current, payments=(replace(source.payments[0], store_hint=str(store.id)),))
        reconcile(store.id, current.now + timedelta(minutes=7))
        finding.refresh_from_db()
        assert finding.state == "reopened" and finding.finding_key == first_key
        assert FindingEvent.objects.filter(finding=finding, event_type="resolved").count() == 1


def test_stale_run_cannot_resolve_a_financial_finding(store, monkeypatch):
    source = snapshot(orders=())
    current = replace(
        source,
        organization_id=str(store.organization_id),
        store_id=str(store.id),
        payments=(replace(source.payments[0], store_hint=str(store.id)),),
    )
    monkeypatch.setattr("modules.findings.service.build_snapshot", lambda store, now: current)
    with tenant_scope(store.organization_id):
        reconcile(store.id, current.now)
        current = replace(current, payments=(), coverage=())
        reconcile(store.id, current.now + timedelta(minutes=10))
        reconcile(store.id, current.now + timedelta(minutes=20))
        assert Finding.objects.get(rule_code="PI-002").state == "open"


def test_browser_csrf_bola_and_idempotency(store, local_crypto):
    user = get_user_model().objects.create_user(username="owner-test", password="test-only-passphrase")
    with tenant_scope(store.organization_id):
        Membership.objects.create(organization_id=store.organization_id, user=user, role="owner")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    session = client.session
    session["authenticated_at"] = time.time()
    session.save()
    url = f"/api/v1/stores/{store.id}/pairing-codes"
    assert (
        client.post(
            url, data="{}", content_type="application/json", HTTP_IDEMPOTENCY_KEY="safe-request-key-123"
        ).status_code
        == 403
    )
    assert client.get(f"/stores/{uuid4()}/").status_code == 404
    assert client.get(f"/stores/{store.id}/").status_code == 200
    csrf = client.cookies["csrftoken"].value
    first = client.post(
        url,
        data="{}",
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
        HTTP_IDEMPOTENCY_KEY="safe-request-key-123",
    )
    assert first.status_code == 201, first.content
    second = client.post(
        url,
        data="{}",
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
        HTTP_IDEMPOTENCY_KEY="safe-request-key-123",
    )
    assert second.json() == first.json()
    outsider = get_user_model().objects.create_user(username="outsider")
    client.force_login(outsider)
    assert client.get(f"/stores/{store.id}/").status_code == 404
