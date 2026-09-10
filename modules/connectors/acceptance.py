"""Bounded test-mode reads through the actual installed App, never financial writes."""

import re

from modules.connectors.stripe import ConnectorFailure, StripeReader

RESOURCES = {
    "payment_intent": ("payment_intents", "pi_"),
    "charge": ("charges", "ch_"),
    "refund": ("refunds", "re_"),
    "event": ("events", "evt_"),
    "checkout.session": ("checkout/sessions", "cs_test_"),
}


def validate_samples(samples: dict[str, str]) -> None:
    if set(samples) != set(RESOURCES) or any(
        not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_]{1,200}", samples[kind])
        for kind, (_, prefix) in RESOURCES.items()
    ):
        raise ValueError("Five exact synthetic test-resource IDs are required")


def probe(reader: StripeReader, samples: dict[str, str]) -> list[str]:
    validate_samples(samples)
    if reader.connector.mode != "test":
        raise ValueError("Provider acceptance reads are restricted to test mode")
    checked = []
    for kind, (path, _) in RESOURCES.items():
        page = reader.get(f"/v1/{path}", {"limit": 1})
        if (
            page.get("object") != "list"
            or not isinstance(page.get("data"), list)
            or not isinstance(page.get("has_more"), bool)
        ):
            raise ConnectorFailure("STRIPE_ACCEPTANCE_LIST_INVALID")
        # Do not retain list payloads or raw resource fields in evidence.
        if any(not isinstance(row, dict) or row.get("livemode") is not False for row in page["data"]):
            raise ConnectorFailure("STRIPE_MODE_CONFLICT", True)
        value = reader.get(f"/v1/{path}/{samples[kind]}")
        if value.get("object") != kind or value.get("id") != samples[kind]:
            raise ConnectorFailure("STRIPE_IDENTITY_CONFLICT", True)
        if value.get("livemode") is not False:
            raise ConnectorFailure("STRIPE_MODE_CONFLICT", True)
        if kind == "event" and value.get("account", reader.connector.account_id) != reader.connector.account_id:
            raise ConnectorFailure("STRIPE_ACCOUNT_CONFLICT", True)
        checked.append(kind)
    return checked
