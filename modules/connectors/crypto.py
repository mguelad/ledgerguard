import base64
import hashlib
import hmac
import json
import os
from datetime import datetime
from typing import Any

import boto3
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from apps.control_plane.errors import Problem
from modules.ingestion.validation import digest, timestamp


def aad(org: str, connector: str, kind: str) -> bytes:
    return json.dumps(
        {"organization_id": org, "connector_id": connector, "environment": settings.ENVIRONMENT, "token_type": kind},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def encrypt_secret(value: dict[str, Any], org: str, connector: str, kind: str) -> dict[str, str]:
    context = aad(org, connector, kind)
    if settings.KMS_KEY_ID:
        result = boto3.client("kms", region_name=settings.AWS_REGION).generate_data_key(
            KeyId=settings.KMS_KEY_ID, KeySpec="AES_256", EncryptionContext={"context": context.decode()}
        )
        key, wrapped = result["Plaintext"], result["CiphertextBlob"]
        provider = "kms"
    else:
        if settings.PRODUCTION or not settings.LOCAL_ENCRYPTION_KEY:
            raise Problem("ENCRYPTION_UNAVAILABLE", 503)
        master = base64.b64decode(settings.LOCAL_ENCRYPTION_KEY, validate=True)
        key = os.urandom(32)
        wrap_nonce = os.urandom(12)
        wrapped = wrap_nonce + AESGCM(master).encrypt(wrap_nonce, key, context)
        provider = "local"
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(value, separators=(",", ":")).encode(), context)
    return {
        "version": "1",
        "provider": provider,
        "key": base64.b64encode(wrapped).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


def decrypt_secret(value: dict[str, str], org: str, connector: str, kind: str) -> dict[str, Any]:
    context = aad(org, connector, kind)
    try:
        if value["version"] != "1":
            raise ValueError("Unsupported version")
        wrapped = base64.b64decode(value["key"], validate=True)
        if value["provider"] == "kms":
            key = boto3.client("kms", region_name=settings.AWS_REGION).decrypt(
                CiphertextBlob=wrapped, KeyId=settings.KMS_KEY_ID, EncryptionContext={"context": context.decode()}
            )["Plaintext"]
        elif value["provider"] == "local" and not settings.PRODUCTION:
            master = base64.b64decode(settings.LOCAL_ENCRYPTION_KEY, validate=True)
            key = AESGCM(master).decrypt(wrapped[:12], wrapped[12:], context)
        else:
            raise ValueError("Unsupported encryption provider")
        plain = AESGCM(key).decrypt(base64.b64decode(value["nonce"]), base64.b64decode(value["ciphertext"]), context)
        result = json.loads(plain)
        if not isinstance(result, dict):
            raise ValueError("Invalid secret")
        return result
    except (KeyError, ValueError, TypeError, InvalidTag):
        raise Problem("CREDENTIAL_UNAVAILABLE", 503) from None


def signature_base(installation_id: str, request_id: str, sent_at: str, body: bytes) -> bytes:
    return f"v1\n{installation_id}\n{request_id}\n{sent_at}\n{digest(body)}".encode()


def verify_woo(
    public_key: bytes,
    signature: str,
    installation_id: str,
    request_id: str,
    sent_at: str,
    body: bytes,
    now: datetime,
    offset_seconds: int = 0,
) -> None:
    if abs((now - timestamp(sent_at)).total_seconds() - offset_seconds) > 300:
        raise Problem("SIGNATURE_EXPIRED", 401)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            base64.b64decode(signature, validate=True), signature_base(installation_id, request_id, sent_at, body)
        )
    except (ValueError, InvalidSignature):
        raise Problem("INVALID_SIGNATURE", 401) from None


def verify_stripe(body: bytes, header: str, secrets: list[str], now: datetime) -> None:
    try:
        fields = [part.split("=", 1) for part in header.split(",")]
        times = [value for key, value in fields if key == "t"]
        if len(times) != 1:
            raise ValueError("Timestamp")
        ts = int(times[0])
        candidates = [value for key, value in fields if key == "v1"]
        if abs(now.timestamp() - ts) > 300:
            raise ValueError("Expired")
        signed = str(ts).encode() + b"." + body
        valid = any(
            hmac.compare_digest(hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest(), candidate)
            for secret in secrets
            for candidate in candidates
        )
        if not valid:
            raise ValueError("Signature")
    except (ValueError, TypeError):
        raise Problem("INVALID_SIGNATURE", 401) from None
