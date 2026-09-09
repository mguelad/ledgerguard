from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from django.utils import timezone

from modules.accounts.tenancy import tenant_scope
from modules.connectors import stripe
from modules.connectors.crypto import decrypt_secret, encrypt_secret
from modules.connectors.models import Installation
from modules.ingestion.models import Cursor, Projection, Scan
from tests.test_connector_journey import pi
from tests.test_database import organization as organization
from tests.test_database import store as store


def token_response(**changes):
    return {
        "scope": "stripe_apps",
        "token_type": "bearer",
        "stripe_user_id": "acct_fixture",
        "livemode": False,
        "access_token": "synthetic-access-token",
        "refresh_token": "synthetic-refresh-token",
    } | changes


def test_app_token_exchange_scopes_and_mode(monkeypatch, settings):
    settings.STRIPE_DEVELOPER_TEST_KEY = "synthetic-developer-secret"
    post = Mock(return_value=httpx.Response(200, json=token_response()))
    monkeypatch.setattr(stripe.httpx, "post", post)
    assert (
        stripe.exchange("test", {"grant_type": "authorization_code", "code": "fixture"})["account_id"] == "acct_fixture"
    )
    assert post.call_args.args == ("https://api.stripe.com/v1/oauth/token",)
    assert post.call_args.kwargs["follow_redirects"] is False
    for changed in [{"scope": "read_write"}, {"livemode": True}, {"token_type": "mac"}, {"refresh_token": ""}]:
        post.return_value = httpx.Response(200, json=token_response(**changed))
        with pytest.raises(stripe.ConnectorFailure) as error:
            stripe.exchange("test", {})
        assert error.value.permanent


def test_uncertain_exchange_is_never_retried(monkeypatch, settings):
    settings.STRIPE_DEVELOPER_TEST_KEY = "synthetic-developer-secret"
    post = Mock(side_effect=httpx.ReadTimeout("fixture timeout"))
    monkeypatch.setattr(stripe.httpx, "post", post)
    with pytest.raises(stripe.ConnectorFailure, match="OAUTH_EXCHANGE_UNCERTAIN") as error:
        stripe.exchange("test", {"grant_type": "refresh_token"})
    assert error.value.permanent and post.call_count == 1


@pytest.mark.parametrize(
    "status,code,permanent",
    [
        (401, "STRIPE_AUTHORIZATION_REVOKED", True),
        (403, "STRIPE_AUTHORIZATION_REVOKED", True),
        (404, "STRIPE_RESOURCE_UNAVAILABLE", False),
        (429, "STRIPE_RATE_LIMITED", False),
        (503, "STRIPE_UNAVAILABLE", False),
        (302, "STRIPE_RESOURCE_UNAVAILABLE", True),
    ],
)
def test_read_failures_preserve_retry_class(monkeypatch, status, code, permanent):
    connector = SimpleNamespace(next_api_at=None, save=Mock())
    monkeypatch.setattr(stripe, "access_token", lambda _: "synthetic-token")
    get = Mock(return_value=httpx.Response(status, headers={"Retry-After": "90"}))
    monkeypatch.setattr(stripe.httpx, "get", get)
    reader = stripe.StripeReader(connector)
    with pytest.raises(stripe.ConnectorFailure, match=code) as error:
        reader.get("/v1/payment_intents/pi_fixture")
    assert error.value.permanent is permanent
    assert get.call_count == 1 and not get.call_args.kwargs["follow_redirects"]
    if status == 429:
        assert error.value.retry_after == 90


def test_read_resource_allowlist_and_stalled_pagination(monkeypatch):
    monkeypatch.setattr(stripe, "access_token", lambda _: "synthetic-token")
    reader = stripe.StripeReader(SimpleNamespace(next_api_at=None, save=Mock()))
    get = Mock()
    monkeypatch.setattr(stripe.httpx, "get", get)
    for path in [
        "https://attacker.example/v1/charges",
        "/v1/charges/ch_one/refund",
        "/v1/customers",
        "/v1/charges/../customers",
    ]:
        with pytest.raises(ValueError):
            reader.get(path)
    get.assert_not_called()
    reader.get = Mock(return_value={"data": [{"id": "pi_same"}], "has_more": True})
    with pytest.raises(stripe.ConnectorFailure, match="STRIPE_PAGINATION_STALLED"):
        list(reader.pages("/v1/payment_intents", {}))
    assert reader.get.call_count == 2


def test_normalization_is_a_strict_privacy_allowlist():
    source = pi("01900000-0000-7000-8000-000000000000")
    connector = SimpleNamespace(mode="test")
    sid, data = stripe.normalize(source, "payment", connector)
    assert sid == "pi_one"
    assert set(data) == {
        "amount_minor",
        "currency",
        "exponent",
        "status",
        "created_at_source",
        "received_minor",
        "capturable_minor",
        "charge_id",
        "method",
        "capture_method",
        "order_hint",
        "store_hint",
    }
    assert "never-store" not in str(data)
    with pytest.raises(stripe.ConnectorFailure, match="STRIPE_MODE_CONFLICT"):
        stripe.normalize(source | {"livemode": True}, "payment", connector)
    _, data = stripe.normalize(
        source | {"metadata": {"order_id": "email@example.com", "ledgerguard_store_id": "invalid"}},
        "payment",
        connector,
    )
    assert data["order_hint"] == data["store_hint"] == ""


def test_resource_fetch_rejects_identity_substitution_and_ignores_non_payment_sessions():
    connector = SimpleNamespace(mode="test")
    reader = SimpleNamespace(
        connector=connector,
        get=Mock(
            return_value={
                "id": "cs_fixture",
                "object": "checkout.session",
                "livemode": False,
                "payment_intent": None,
                "amount_total": None,
            }
        ),
    )
    assert stripe.fetch_resource(reader, "checkout.session", "cs_fixture") is None
    reader.get.return_value = {
        "id": "cs_different",
        "object": "checkout.session",
        "livemode": False,
        "payment_intent": None,
    }
    with pytest.raises(stripe.ConnectorFailure, match="STRIPE_IDENTITY_CONFLICT") as error:
        stripe.fetch_resource(reader, "checkout.session", "cs_fixture")
    assert error.value.permanent
    with pytest.raises(stripe.ConnectorFailure, match="STRIPE_ID_INVALID") as error:
        stripe.fetch_resource(reader, "checkout.session", "pi_fixture")
    assert error.value.permanent


@pytest.fixture
def stripe_installation(store, local_crypto):
    with tenant_scope(store.organization_id):
        connector = Installation.objects.create(
            organization_id=store.organization_id, kind="stripe", mode="test", account_id="acct_fixture"
        )
        connector.credential_ciphertext = encrypt_secret(
            {"access_token": "synthetic-old-token", "refresh_token": "synthetic-old-refresh"},
            str(store.organization_id),
            str(connector.id),
            "oauth",
        )
        connector.token_expires_at = timezone.now() - timedelta(seconds=1)
        connector.save()
        return connector


@pytest.mark.integration
@pytest.mark.django_db
def test_rotation_persists_the_whole_pair_atomically(stripe_installation, monkeypatch):
    connector = stripe_installation
    exchange = Mock(
        return_value={
            "account_id": connector.account_id,
            "access_token": "synthetic-new-token",
            "refresh_token": "synthetic-new-refresh",
        }
    )
    monkeypatch.setattr(stripe, "exchange", exchange)
    with tenant_scope(connector.organization_id):
        row = Installation.objects.select_for_update().get(id=connector.id)
        assert stripe.access_token(row) == "synthetic-new-token"
        row.refresh_from_db()
        assert stripe.access_token(row) == "synthetic-new-token" and exchange.call_count == 1
        assert (
            decrypt_secret(row.credential_ciphertext, str(row.organization_id), str(row.id), "oauth")["refresh_token"]
            == "synthetic-new-refresh"
        )


@pytest.mark.integration
@pytest.mark.django_db
def test_scan_coverage_advances_only_after_all_phases(stripe_installation, monkeypatch):
    connector = stripe_installation
    monkeypatch.setattr(stripe, "access_token", lambda _: "synthetic-token")
    reads = []

    def get(self, path, params=None):
        reads.append(path)
        return {"data": [], "has_more": False}

    monkeypatch.setattr(stripe.StripeReader, "get", get)
    with tenant_scope(connector.organization_id):
        stripe.poll(connector.id, repair=True)
        scan = Scan.objects.get(connector=connector)
        stripe.poll(connector.id)
        assert Scan.objects.filter(connector=connector).count() == 1
        for _ in range(4):
            stripe.scan_page(scan.id)
            assert not Cursor.objects.filter(connector=connector, complete=True).exists()
        stripe.scan_page(scan.id)
        scan.refresh_from_db()
        assert scan.status == "complete"
        assert Cursor.objects.filter(connector=connector, complete=True).count() == 2
        assert reads == ["/v1/events", "/v1/payment_intents", "/v1/refunds", "/v1/checkout/sessions"]
        assert not Projection.objects.filter(connector=connector).exists()
