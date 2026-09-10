import io
import json
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from modules.accounts import maintenance
from modules.accounts.erasure_receipts import parse_receipt
from modules.accounts.management.commands import replay_erasures
from modules.accounts.models import Organization, TenantDirectory
from modules.accounts.tenancy import tenant_scope
from modules.findings.models import Finding
from tests.test_database import organization as organization
from tests.test_database import store as store
from tests.test_operations import case as case
from tests.test_operations import maintenance_role


def receipt(org):
    return {"organization_id": str(org), "erased_at": "2026-09-09T12:00:00+00:00", "digest": "a" * 64}


@pytest.mark.parametrize(
    "change",
    [
        {"digest": "not-a-digest"},
        {"erased_at": "2026-09-09T12:00:00"},
        {"organization_id": "invalid"},
        {"extra": "do-not-store"},
        {"digest": None},
        {"erased_at": 123},
    ],
)
def test_invalid_receipt_cannot_authorize_erasure(change):
    org = uuid4()
    with pytest.raises(ValueError, match="Invalid external"):
        parse_receipt(f"erasures/{org}.json", json.dumps(receipt(org) | change).encode())


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b"x" * 4097, b"null"])
def test_malformed_and_oversize_receipts_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_receipt("erasures/fixture.json", raw)


@pytest.mark.integration
@pytest.mark.django_db
def test_restore_replay_uses_read_only_ledger_and_is_idempotent(case, settings, tmp_path, monkeypatch):
    settings.BASE_DIR, settings.REPORT_BUCKET = tmp_path, ""
    settings.DELETION_LEDGER_BUCKET = "external-ledger"
    org = case.org
    s3 = Mock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": f"erasures/{org}.json"}]}]
    s3.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(json.dumps(receipt(org)).encode())}
    s3.put_object.side_effect = AssertionError("Restore must never write the external ledger")
    monkeypatch.setattr(replay_erasures.boto3, "client", lambda *a, **kw: s3)
    kwargs = {
        "expected_database_host": connection.settings_dict["HOST"],
        "expected_database_name": connection.settings_dict["NAME"],
    }
    with maintenance_role():
        call_command("replay_erasures", dry_run=True, **kwargs, stdout=io.StringIO())
        with tenant_scope(org):
            assert Organization.objects.get(id=org).active and Finding.objects.exists()
        output = io.StringIO()
        call_command("replay_erasures", confirm_isolated_restore=True, **kwargs, stdout=output)
        assert "reapplied: 1" in output.getvalue()
        with tenant_scope(org):
            assert not Organization.objects.exists() and not Finding.objects.exists()
        directory = TenantDirectory.objects.get(id=org)
        assert directory.erased_at == datetime(2026, 9, 9, 12, tzinfo=UTC)
        assert directory.erasure_digest == "a" * 64
        output = io.StringIO()
        call_command("replay_erasures", confirm_isolated_restore=True, **kwargs, stdout=output)
        assert "reapplied: 0" in output.getvalue()
    s3.put_object.assert_not_called()


@pytest.mark.integration
@pytest.mark.django_db
def test_replay_refuses_wrong_database_before_s3_access(case, settings, monkeypatch):
    s3 = Mock()
    monkeypatch.setattr(replay_erasures.boto3, "client", s3)
    with maintenance_role(), pytest.raises(CommandError, match="does not match"):
        call_command(
            "replay_erasures",
            confirm_isolated_restore=True,
            expected_database_host="wrong-db",
            expected_database_name=connection.settings_dict["NAME"],
        )
    s3.assert_not_called()


@pytest.mark.integration
@pytest.mark.django_db
def test_erasure_identity_is_checked_before_any_deletion(case):
    other = uuid4()
    parsed = parse_receipt(f"erasures/{other}.json", json.dumps(receipt(other)).encode())
    with maintenance_role(), pytest.raises(ValueError, match="identity mismatch"):
        maintenance.erase_tenant(case.org, replay_receipt=parsed)
    with tenant_scope(case.org):
        assert Organization.objects.exists() and Finding.objects.exists()
