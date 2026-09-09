import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import resolve
from django.utils import timezone
from jsonschema import validate

from modules.accounts.tenancy import tenant_scope
from modules.connectors import stripe
from modules.connectors.crypto import signature_base
from modules.connectors.models import Installation, OAuthState, Store
from modules.findings.models import Finding
from modules.findings.service import build_snapshot, reconcile
from modules.ingestion.models import Cursor, Projection, Receipt
from modules.ingestion.validation import utc

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def operator(local_crypto):
    user = get_user_model().objects.create_user(
        username="integration-owner", email="owner@example.com", password="synthetic-local-password"
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    session = client.session
    session["authenticated_at"] = time.time()
    session.save()
    assert client.get("/").status_code == 200
    return user, client


def contract_response(path, response):
    if response.get("Content-Type", "").split(";")[0] not in {"application/json", "application/problem+json"}:
        return response
    document = json.loads((Path(__file__).parents[1] / "contracts/openapi.json").read_text())
    name = resolve(path.split("?")[0]).func.__name__
    operation = next(
        op for methods in document["paths"].values() for op in methods.values() if op["operationId"] == name
    )
    if response.status_code >= 400:
        schema = document["components"]["schemas"]["Problem"]
    else:
        schema = operation["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
    validate(response.json(), schema)
    return response


def post(client, path, value, version=None, key=None):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value, "HTTP_IDEMPOTENCY_KEY": key or str(uuid4())}
    if version is not None:
        headers["HTTP_IF_MATCH"] = f'"{version}"'
    return contract_response(
        path, client.post(path, data=json.dumps(value), content_type="application/json", **headers)
    )


def machine(client, path, value, key, key_id):
    body = json.dumps(value, separators=(",", ":")).encode()
    signature = base64.b64encode(
        key.sign(signature_base(value.get("installation_id", "claim"), value["request_id"], value["sent_at"], body))
    ).decode()
    return contract_response(
        path,
        client.post(
            path,
            data=body,
            content_type="application/json",
            HTTP_X_LEDGERGUARD_SIGNATURE=signature,
            HTTP_X_LEDGERGUARD_KEY_ID=key_id,
        ),
    )


def pi(store_id):
    return {
        "id": "pi_one",
        "object": "payment_intent",
        "livemode": False,
        "amount": 12995,
        "amount_received": 12995,
        "amount_capturable": 0,
        "currency": "eur",
        "created": int((timezone.now() - timedelta(hours=3)).timestamp()),
        "status": "succeeded",
        "capture_method": "automatic",
        "latest_charge": "ch_one",
        "payment_method_types": ["card"],
        "metadata": {"order_id": "1", "ledgerguard_store_id": str(store_id), "email": "never-store@example.com"},
        "customer": {"email": "never-store@example.com"},
    }


def test_complete_signed_onboarding_and_findings(operator, monkeypatch, settings):
    user, client = operator
    first = post(client, "/api/v1/organizations", {"name": "Northwind"}, key="organization-replay-001")
    assert first.status_code == 201, first.content
    org = UUID(first.json()["id"])
    assert (
        post(client, "/api/v1/organizations", {"name": "Northwind"}, key="organization-replay-001").json()
        == first.json()
    )
    assert (
        post(client, "/api/v1/organizations", {"name": "Different"}, key="organization-replay-001").status_code == 409
    )
    result = post(
        client, f"/api/v1/organizations/{org}/stores", {"name": "Shop", "hostname": "shop.example.com", "mode": "test"}
    )
    assert result.status_code == 201, result.content
    store_id = UUID(result.json()["id"])
    settings.STRIPE_TEST_CLIENT_ID = "ca_fixture"
    authorization = client.get(f"/api/v1/stripe/oauth/start?store_id={store_id}")
    assert authorization.status_code == 302 and urlparse(authorization.url).hostname == "marketplace.stripe.com"
    state = parse_qs(urlparse(authorization.url).query)["state"][0]
    monkeypatch.setattr(
        stripe,
        "exchange",
        lambda mode, grant: {
            "account_id": "acct_one",
            "access_token": "synthetic-access-token",
            "refresh_token": "synthetic-refresh-token",
        },
    )
    assert client.get("/oauth/stripe/callback", {"state": state, "code": "one-use-code"}).status_code == 302
    assert client.get("/oauth/stripe/callback", {"state": state, "code": "one-use-code"}).status_code == 400
    with tenant_scope(org):
        connected = Installation.objects.get(kind="stripe")
        assert "synthetic-access-token" not in json.dumps(connected.credential_ciphertext)
    code = post(client, f"/api/v1/stores/{store_id}/pairing-codes", {}).json()["pairing_code"]
    key, key_id = Ed25519PrivateKey.generate(), str(uuid4())
    claim = {
        "schema_version": "1.0",
        "request_id": str(uuid4()),
        "pairing_code": code,
        "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
        "key_id": key_id,
        "sent_at": utc(timezone.now()),
        "mode": "test",
        "versions": {
            "plugin": "0.1.0",
            "wordpress": "7.1",
            "woocommerce": "11.1.0",
            "gateway": "11.0.0",
            "php": "8.3.6",
        },
        "capabilities": ["orders", "refunds", "hpos", "delta", "repair"],
    }
    claimed = machine(client, "/api/v1/plugin/installations/claim", claim, key, key_id)
    assert claimed.status_code == 201, claimed.content
    installation_id = claimed.json()["installation_id"]
    assert (
        machine(client, "/api/v1/plugin/installations/claim", claim, key, key_id).json()["installation_id"]
        == installation_id
    )
    before = utc(timezone.now() - timedelta(hours=3))
    order = {
        "kind": "order",
        "source_id": "1",
        "revision": before,
        "created_at": before,
        "status": "pending",
        "amount": "129.95",
        "total_refunded": "0",
        "refund_ids": [],
        "currency": "EUR",
        "payment_method": "stripe",
        "transaction_id": "pi_one",
        "stripe_ids": {"payment_intent": "pi_one"},
        "paid_at": None,
    }
    envelope = {
        "schema_version": "1.0",
        "request_id": str(uuid4()),
        "installation_id": installation_id,
        "sent_at": utc(timezone.now()),
        "covered_from": utc(timezone.now() - timedelta(days=35)),
        "covered_through": utc(timezone.now() - timedelta(minutes=1)),
        "scan_id": str(uuid4()),
        "page": 1,
        "final_page": True,
        "records": [order],
    }
    accepted = machine(client, "/api/v1/plugin/facts/batch", envelope, key, key_id)
    assert accepted.status_code == 200 and accepted.json()["coverage_advanced"], accepted.content
    assert machine(client, "/api/v1/plugin/facts/batch", envelope, key, key_id).json() == accepted.json()
    assert (
        machine(
            client, "/api/v1/plugin/facts/batch", envelope | {"records": [order | {"amount": "1"}]}, key, key_id
        ).status_code
        == 409
    )
    heartbeat = {k: envelope[k] for k in ["schema_version", "installation_id", "sent_at"]} | {
        "request_id": str(uuid4()),
        "queue_depth": 0,
        "hpos": True,
        "clock_offset_seconds": 0,
        "versions": claim["versions"],
    }
    assert machine(client, "/api/v1/plugin/heartbeat", heartbeat, key, key_id).status_code == 200
    next_key, next_id = Ed25519PrivateKey.generate(), str(uuid4())
    rotation = {k: envelope[k] for k in ["schema_version", "installation_id", "sent_at"]} | {
        "request_id": str(uuid4()),
        "new_key_id": next_id,
        "new_public_key": base64.b64encode(next_key.public_key().public_bytes_raw()).decode(),
    }
    assert machine(client, "/api/v1/plugin/keys/rotate", rotation, key, key_id).status_code == 200
    assert (
        machine(client, "/api/v1/plugin/keys/rotate", rotation | {"request_id": str(uuid4())}, key, key_id).status_code
        == 200
    )
    assert (
        machine(
            client, "/api/v1/plugin/heartbeat", heartbeat | {"request_id": str(uuid4())}, next_key, next_id
        ).status_code
        == 200
    )
    monkeypatch.setattr(stripe.StripeReader, "get", lambda reader, path, params=None: pi(store_id))
    result = post(client, f"/api/v1/stores/{store_id}/verify-link", {"order_id": "1"})
    assert result.status_code == 200 and result.json()["status"] == "verified", result.content
    with tenant_scope(org):
        connected.refresh_from_db()
        Projection.objects.filter(connector=connected).update(
            revision=timezone.now() - timedelta(hours=2), succeeded_at=timezone.now() - timedelta(hours=2)
        )
        for source in ["stripe_payments", "stripe_refunds"]:
            Cursor.objects.create(
                organization_id=org,
                connector=connected,
                object_class=source,
                covered_from=timezone.now() - timedelta(days=35),
                covered_through=timezone.now() - timedelta(seconds=10),
                observed_at=timezone.now(),
                complete=True,
            )
        store = Store.objects.get(id=store_id)
        assert build_snapshot(store, timezone.now()).link_verified
        reconcile(store_id)
        finding = Finding.objects.get(rule_code="PI-001")
        assert "never-store@example.com" not in json.dumps(finding.evidence)
    for path in [
        f"/findings/{finding.id}/",
        f"/stores/{store_id}/",
        f"/o/{org}/",
        f"/o/{org}/settings/",
        "/api/v1/reconciliation-runs",
        f"/api/v1/stores/{store_id}/findings",
    ]:
        response = client.get(path)
        assert response.status_code == 200, (path, response.content)
    result = post(client, f"/api/v1/findings/{finding.id}/acknowledge", {}, finding.lock_version)
    assert result.json()["state"] == "acknowledged"
    for action, data in [
        ("suppress", {"note": "Provider case under review", "until": utc(timezone.now() + timedelta(hours=1))}),
        ("note", {"note": "Exact identifiers checked"}),
        ("resolve", {"note": "Correction recorded in source"}),
    ]:
        result = post(client, f"/api/v1/findings/{finding.id}/{action}", data, result.json()["lock_version"])
        assert result.status_code == 200, result.content
    assert post(client, f"/api/v1/findings/{finding.id}/note", {"note": "stale update"}, 1).status_code == 409
    with tenant_scope(org):
        store.refresh_from_db()
    result = post(
        client,
        f"/api/v1/stores/{store_id}/policy",
        {"review_dry_run": True, "alerts_enabled": True, "disabled_rules": ["PI-008"], "instant_grace_minutes": 10},
        store.lock_version,
    )
    assert result.status_code == 200, result.content
    secret = "whsec_" + "a" * 32
    result = post(
        client, f"/api/v1/connectors/{connected.id}/webhook-secret", {"signing_secret": secret}, connected.lock_version
    )
    assert result.status_code == 200, result.content
    event = {
        "id": "evt_one",
        "livemode": False,
        "account": "acct_one",
        "type": "payment_intent.succeeded",
        "created": int(timezone.now().timestamp()),
        "data": {"object": pi(store_id)},
    }
    raw = json.dumps(event).encode()
    ts = int(timezone.now().timestamp())
    sig = hmac.new(secret.encode(), str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()
    webhook = client.post(
        urlparse(result.json()["destination_url"]).path,
        data=raw,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=f"t={ts},v1={sig}",
    )
    assert webhook.status_code == 200, webhook.content
    duplicate = client.post(
        urlparse(result.json()["destination_url"]).path,
        data=raw,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=f"t={ts},v1={sig}",
    )
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"
    conflicting_raw = json.dumps(event | {"type": "payment_intent.processing"}).encode()
    conflicting_sig = hmac.new(secret.encode(), str(ts).encode() + b"." + conflicting_raw, hashlib.sha256).hexdigest()
    conflict = client.post(
        urlparse(result.json()["destination_url"]).path,
        data=conflicting_raw,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=f"t={ts},v1={conflicting_sig}",
    )
    assert conflict.status_code == 409
    with tenant_scope(org):
        receipt = Receipt.objects.get(external_id="evt_one")
        stripe.process_receipt(receipt.id)
        receipt.refresh_from_db()
        assert receipt.status == "normalized"
    assert (
        post(client, f"/api/v1/connectors/{connected.id}/disconnect", {}, result.json()["lock_version"]).status_code
        == 200
    )
    with tenant_scope(org):
        connected.refresh_from_db()
        assert not connected.credential_ciphertext and not connected.webhook_ciphertext


def test_oauth_uncertainty_consumes_state(operator, settings, monkeypatch):
    _, client = operator
    org = post(client, "/api/v1/organizations", {"name": "Failure boundary"}).json()["id"]
    store = post(
        client,
        f"/api/v1/organizations/{org}/stores",
        {"name": "Shop", "hostname": "failure.example.com", "mode": "test"},
    ).json()["id"]
    settings.STRIPE_TEST_CLIENT_ID = "ca_test"
    state = parse_qs(urlparse(client.get(f"/api/v1/stripe/oauth/start?store_id={store}").url).query)["state"][0]

    def uncertain(*args):
        raise stripe.ConnectorFailure("OAUTH_EXCHANGE_UNCERTAIN", True)

    monkeypatch.setattr(stripe, "exchange", uncertain)
    assert client.get("/oauth/stripe/callback", {"state": state, "code": "code"}).status_code == 503
    with tenant_scope(org):
        assert OAuthState.objects.get().consumed_at is not None and not Installation.objects.exists()
