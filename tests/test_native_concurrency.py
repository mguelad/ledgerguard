"""These checks require native PostgreSQL sockets and independent transactions."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from django.db import connection, connections

from modules.accounts.models import Organization, TenantDirectory
from modules.accounts.tenancy import tenant_scope
from modules.findings.models import Outbox
from modules.ingestion.service import enqueue
from workers import runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LEDGERGUARD_PGLITE") == "1",
        reason="Native role authentication and concurrent database sessions required",
    ),
]


def test_runtime_credentials_cannot_assume_the_migration_role(django_db_setup):
    cfg = connection.settings_dict
    with psycopg.connect(
        host=cfg["HOST"],
        port=cfg["PORT"],
        dbname=cfg["NAME"],
        user="ledgerguard_app",
        password="local-development",
        sslmode=os.environ.get("DB_SSLMODE", "disable"),
        autocommit=True,
    ) as db:
        assert db.execute("SELECT current_user").fetchone() == ("ledgerguard_app",)
        assert db.execute("SELECT count(*) FROM accounts_organization").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SET ROLE ledgerguard_migrator")


def test_two_workers_commit_one_effect(django_db_setup, django_db_blocker, monkeypatch):
    org, resource = uuid4(), uuid4()
    effects = []
    start = threading.Barrier(2)
    entered, release = threading.Event(), threading.Event()

    def handle(row):
        effects.append(row.id)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test coordinator did not release the worker")

    monkeypatch.setattr(runtime, "handle", handle)
    with django_db_blocker.unblock():
        with tenant_scope(org):
            TenantDirectory.objects.create(id=org)
            Organization.objects.create(id=org, organization_id=org, name="Concurrent test")
            enqueue(org, "reconcile_store", resource, "native:" + str(resource))
            row = Outbox.objects.get(resource_id=resource)
            message = runtime.message(row)

        def work():
            try:
                start.wait(5)
                return runtime.run_message(message)
            finally:
                connections.close_all()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(work), pool.submit(work)
                assert entered.wait(5)
                release.set()
                assert first.result(timeout=10) is True
                assert second.result(timeout=10) is True
            assert effects == [row.id]
            with tenant_scope(org):
                row.refresh_from_db()
                assert row.status == "done" and row.attempts == 0
        finally:
            release.set()
            with tenant_scope(org):
                Outbox.objects.filter(id=row.id).delete()
                Organization.objects.filter(id=org).delete()
                TenantDirectory.objects.filter(id=org).delete()
