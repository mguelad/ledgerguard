import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from modules.accounts.tenancy import tenant_scope
from modules.connectors import acceptance, stripe
from modules.connectors.crypto import decrypt_secret
from tests.test_database import organization as organization
from tests.test_database import store as store
from tests.test_stripe_client import stripe_installation as stripe_installation

SAMPLES = {kind: prefix + "synthetic" for kind, (_, prefix) in acceptance.RESOURCES.items()}


def get(path, params=None):
    if params:
        return {"object": "list", "data": [{"livemode": False}], "has_more": False}
    kind = next(kind for kind, (resource, _) in acceptance.RESOURCES.items() if path.startswith(f"/v1/{resource}/"))
    return {"object": kind, "id": SAMPLES[kind], "livemode": False, "private_customer_value": "do-not-emit"}


def test_probe_is_ten_bounded_reads_without_retaining_source_payloads():
    reader = SimpleNamespace(
        connector=SimpleNamespace(mode="test", account_id="acct_fixture"), get=Mock(side_effect=get)
    )
    result = acceptance.probe(reader, SAMPLES)
    assert result == list(acceptance.RESOURCES) and reader.get.call_count == 10
    assert "do-not-emit" not in str(result)
    assert all(call.args[1] == {"limit": 1} for call in reader.get.call_args_list[::2])


@pytest.mark.parametrize(
    "result,code",
    [
        ({"object": "not-a-list"}, "STRIPE_ACCEPTANCE_LIST_INVALID"),
        ({"object": "list", "data": [{"livemode": True}], "has_more": False}, "STRIPE_MODE_CONFLICT"),
    ],
)
def test_probe_rejects_invalid_or_live_lists(result, code):
    reader = SimpleNamespace(connector=SimpleNamespace(mode="test"), get=Mock(return_value=result))
    with pytest.raises(stripe.ConnectorFailure, match=code):
        acceptance.probe(reader, SAMPLES)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"id": "pi_wrong"}, "STRIPE_IDENTITY_CONFLICT"),
        ({"livemode": True}, "STRIPE_MODE_CONFLICT"),
        ({"account": "acct_other"}, "STRIPE_ACCOUNT_CONFLICT"),
    ],
)
def test_probe_rejects_identity_mode_and_event_account_substitution(change, code):
    def changed(path, params=None):
        value = get(path, params)
        if not params and ("account" not in change or value["object"] == "event"):
            value |= change
        return value

    reader = SimpleNamespace(
        connector=SimpleNamespace(mode="test", account_id="acct_fixture"), get=Mock(side_effect=changed)
    )
    with pytest.raises(stripe.ConnectorFailure, match=code):
        acceptance.probe(reader, SAMPLES)


def test_probe_rejects_live_namespace_and_bad_ids_before_reads():
    reader = SimpleNamespace(connector=SimpleNamespace(mode="live"), get=Mock())
    with pytest.raises(ValueError):
        acceptance.probe(reader, SAMPLES)
    with pytest.raises(ValueError):
        acceptance.probe(reader, SAMPLES | {"refund": "re_invalid/path"})
    reader.get.assert_not_called()


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize(
    "error",
    [None, stripe.ConnectorFailure("STRIPE_UNAVAILABLE"), stripe.ConnectorFailure("STRIPE_MODE_CONFLICT", True)],
)
def test_installed_probe_persists_rotation_and_redacts_evidence(
    stripe_installation, settings, monkeypatch, tmp_path, error
):
    connector = stripe_installation
    settings.ENVIRONMENT = "development"
    monkeypatch.setattr(
        stripe,
        "exchange",
        Mock(
            return_value={
                "account_id": connector.account_id,
                "access_token": "synthetic-new-token",
                "refresh_token": "synthetic-new-refresh",
            }
        ),
    )
    read = Mock(side_effect=error) if error else Mock(side_effect=get)
    monkeypatch.setattr(stripe.StripeReader, "get", lambda self, *a, **kw: read(*a, **kw))
    kwargs = {kind.replace(".", "_") + "_id": value for kind, value in SAMPLES.items()}
    output = tmp_path / "probe.json"
    kwargs.update(
        organization=connector.organization_id,
        installation=connector.id,
        confirm_test_account=connector.account_id,
        rotate_token=True,
        output=output,
        stdout=io.StringIO(),
    )
    if error:
        with pytest.raises(CommandError, match=error.code):
            call_command("check_stripe_readiness", **kwargs)
    else:
        call_command("check_stripe_readiness", **kwargs)
    report = json.loads(output.read_text())
    assert report["passed"] is (error is None) and report["production_accepted"] is False
    assert report["tokens_rotated"] is True
    assert "do-not-emit" not in output.read_text() and "synthetic-new-token" not in output.read_text()
    with tenant_scope(connector.organization_id):
        connector.refresh_from_db()
        if error and error.permanent:
            assert connector.status == "suspended" and not connector.credential_ciphertext
        else:
            assert (
                decrypt_secret(
                    connector.credential_ciphertext, str(connector.organization_id), str(connector.id), "oauth"
                )["refresh_token"]
                == "synthetic-new-refresh"
            )


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize(
    "override,environment",
    [
        ({}, "production"),
        ({"confirm_test_account": "invalid"}, "staging"),
        ({"refund_id": "bad-id"}, "staging"),
        ({"organization": uuid4()}, "staging"),
        ({"installation": uuid4()}, "staging"),
    ],
)
def test_provider_checks_require_explicit_matching_test_scope(
    stripe_installation, settings, monkeypatch, tmp_path, override, environment
):
    settings.ENVIRONMENT = environment
    exchange = Mock()
    monkeypatch.setattr(stripe, "exchange", exchange)
    kwargs = {kind.replace(".", "_") + "_id": value for kind, value in SAMPLES.items()}
    kwargs.update(
        organization=stripe_installation.organization_id,
        installation=stripe_installation.id,
        confirm_test_account=stripe_installation.account_id,
        output=tmp_path / "probe.json",
    )
    with pytest.raises(CommandError):
        call_command("check_stripe_readiness", **(kwargs | override))
    exchange.assert_not_called()


def test_reader_treats_malformed_provider_json_as_retryable_schema_failure(monkeypatch):
    monkeypatch.setattr(stripe, "access_token", lambda _: "synthetic-token")
    monkeypatch.setattr(stripe.httpx, "get", Mock(return_value=httpx.Response(200, content=b"not-json")))
    reader = stripe.StripeReader(SimpleNamespace(next_api_at=None, save=Mock()))
    with pytest.raises(stripe.ConnectorFailure, match="STRIPE_SCHEMA_INVALID"):
        reader.get("/v1/payment_intents")
