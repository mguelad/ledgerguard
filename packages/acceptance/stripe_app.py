"""LedgerGuard's least-privilege Stripe App deployment contract."""

import re
from typing import Any
from urllib.parse import urlsplit

PERMISSIONS = {
    "payment_intent_read": "Compare payment status and amounts with WooCommerce orders.",
    "charge_read": "Read charges and refunds to detect payment and refund discrepancies.",
    "checkout_session_read": "Link Checkout payments to the exact WooCommerce order.",
    "event_read": "Recover payment changes missed during webhook delivery interruptions.",
}


def https_origin(value: str) -> str:
    url = urlsplit(value)
    if (
        not re.fullmatch(r"https://[a-z0-9]+(?:[.-][a-z0-9]+)*(?::443)?", value)
        or not url.hostname
        or "." not in url.hostname
        or url.hostname.endswith((".invalid", ".localhost", ".local"))
        or re.fullmatch(r"[0-9.]+", url.hostname)
    ):
        raise ValueError("An exact public HTTPS DNS origin without path, query or credentials is required")
    return value


def manifest(app_id: str, origin: str, version: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+", app_id) or app_id.startswith("com.example."):
        raise ValueError("Supply the operator-selected unique Stripe App ID, not an example ID")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("A semantic release version is required")
    return {
        "id": app_id,
        "name": "LedgerGuard",
        "version": version,
        "distribution_type": "public",
        "stripe_api_access_type": "oauth",
        "allowed_redirect_uris": [https_origin(origin) + "/oauth/stripe/callback"],
        # Legacy test mode is supported; managed sandbox credentials are a distinct
        # Stripe environment and must be qualified before advertising compatibility.
        "sandbox_install_compatible": False,
        "permissions": [{"permission": key, "purpose": value} for key, value in PERMISSIONS.items()],
    }


def validate_manifest(value: dict[str, Any], app_id: str, origin: str, version: str) -> None:
    if value != manifest(app_id, origin, version):
        raise ValueError("Stripe App manifest differs from the reviewed OAuth/permission/callback contract")
