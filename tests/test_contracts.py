import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.control_plane.errors import Problem
from modules.connectors.crypto import decrypt_secret, encrypt_secret, signature_base, verify_stripe, verify_woo
from modules.ingestion.validation import parse_body, utc, validate, validator

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"\xff", b'{"a":' + b"[" * 13 + b"0" + b"]" * 13 + b"}"]
)
def test_json_rejects_ambiguous_or_excessive_input(raw):
    with pytest.raises(Problem):
        parse_body(raw)


def test_body_size_bound():
    with pytest.raises(Problem, match="PAYLOAD_TOO_LARGE"):
        parse_body(b" " * 1_048_577)


def test_all_schemas_are_valid(settings):
    for path in (settings.BASE_DIR / "contracts/schemas").glob("*.json"):
        validator(path.name)


def test_signed_bytes_identity_and_expiry():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    request, installation = str(uuid4()), str(uuid4())
    body, sent = b'{"value":1}', utc(NOW)
    signature = base64.b64encode(key.sign(signature_base(installation, request, sent, body))).decode()
    verify_woo(public, signature, installation, request, sent, body, NOW)
    for changed in [b'{"value":2}', b'{ "value":1}']:
        with pytest.raises(Problem):
            verify_woo(public, signature, installation, request, sent, changed, NOW)
    with pytest.raises(Problem):
        verify_woo(public, signature, str(uuid4()), request, sent, body, NOW)
    with pytest.raises(Problem, match="SIGNATURE_EXPIRED"):
        verify_woo(public, signature, installation, request, sent, body, NOW + timedelta(seconds=301))


def test_stripe_signature_rotation_and_raw_body():
    body, secret, ts = b'{"id":"evt_one"}', "whsec_test_secret", int(NOW.timestamp())
    signature = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={signature}"
    verify_stripe(body, header, ["previous", secret], NOW)
    for invalid in [header + f",t={ts}", "t=0,v1=" + signature, "bad", "t=invalid,v1=00"]:
        with pytest.raises(Problem):
            verify_stripe(body, invalid, [secret], NOW)
    with pytest.raises(Problem):
        verify_stripe(body + b" ", header, [secret], NOW)


def test_credential_context_binding(local_crypto):
    secret = {"refresh_token": "synthetic-refresh-value"}
    sealed = encrypt_secret(secret, "organization-one", "connector-one", "oauth")
    assert "synthetic-refresh-value" not in json.dumps(sealed)
    assert decrypt_secret(sealed, "organization-one", "connector-one", "oauth") == secret
    for org, connector, kind in [
        ("organization-two", "connector-one", "oauth"),
        ("organization-one", "connector-two", "oauth"),
        ("organization-one", "connector-one", "webhook"),
    ]:
        with pytest.raises(Problem):
            decrypt_secret(sealed, org, connector, kind)


def test_queue_rejects_business_payload():
    value = {
        "schema_version": 1,
        "message_id": str(uuid4()),
        "task_type": "reconcile_store",
        "organization_id": str(uuid4()),
        "resource_id": str(uuid4()),
        "attempt_context": 0,
        "traceparent": "",
    }
    validate(value, "queue-1.json")
    with pytest.raises(Problem):
        validate(value | {"customer_email": "excluded@example.com"}, "queue-1.json")


def test_shared_php_signature_vector(settings):
    vector = json.loads((settings.BASE_DIR / "fixtures/contracts/woo-signature.json").read_text())
    base = signature_base(vector["installation_id"], vector["request_id"], vector["sent_at"], vector["body"].encode())
    assert base.decode() == vector["signature_base"]
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(vector["seed_hex"]))
    assert base64.b64encode(key.sign(base)).decode() == vector["signature"]
    verify_woo(
        base64.b64decode(vector["public_key"]),
        vector["signature"],
        vector["installation_id"],
        vector["request_id"],
        vector["sent_at"],
        vector["body"].encode(),
        datetime.fromisoformat(vector["sent_at"]),
    )
