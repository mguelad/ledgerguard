"""Read-only Stripe resource access and separately scoped OAuth token exchange."""

import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.control_plane.errors import Problem
from modules.accounts.tenancy import audit
from modules.connectors.crypto import decrypt_secret, encrypt_secret
from modules.connectors.models import Installation, OAuthState, ResourceLocator, Store, StoreStripeLink
from modules.ingestion.models import Cursor, Projection, Receipt
from modules.ingestion.service import advance_stripe_coverage, reconcile_later, upsert
from modules.ingestion.validation import digest, utc
from packages.reconciliation_core.money import parse_stripe

RESOURCE_PATH = re.compile(r"^/v1/(?:payment_intents|charges|refunds|events|checkout/sessions)(?:/[A-Za-z0-9_]+)?$")
OBJECT_ID = re.compile(r"^(?:pi|ch|re|evt|cs)_[A-Za-z0-9_]{1,240}$")
OBJECT_PREFIXES = {
    "payment_intent": "pi_",
    "charge": "ch_",
    "refund": "re_",
    "checkout.session": "cs_",
}
STATUSES = {
    "payment": {
        "requires_payment_method",
        "requires_confirmation",
        "requires_action",
        "processing",
        "requires_capture",
        "canceled",
        "succeeded",
    },
    "stripe_refund": {"pending", "requires_action", "succeeded", "failed", "canceled"},
    "session": {"paid", "unpaid", "no_payment_required"},
}


class ConnectorFailure(Problem):
    def __init__(self, code: str, permanent: bool = False, retry_after: int = 0) -> None:
        super().__init__(code, 503)
        self.permanent, self.retry_after = permanent, retry_after


def developer_key(mode: str) -> str:
    key = settings.STRIPE_DEVELOPER_LIVE_KEY if mode == "live" else settings.STRIPE_DEVELOPER_TEST_KEY
    if not key:
        raise Problem("STRIPE_APP_NOT_CONFIGURED", 503)
    return str(key)


def exchange(mode: str, grant: dict[str, str]) -> dict[str, Any]:
    try:
        response = httpx.post(
            "https://api.stripe.com/v1/oauth/token",
            auth=(developer_key(mode), ""),
            data=grant,
            timeout=20,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise ConnectorFailure("OAUTH_EXCHANGE_UNCERTAIN", True) from None
    if response.status_code != 200:
        # A token exchange is not retried: a lost response can have rotated the refresh token.
        raise ConnectorFailure("OAUTH_REAUTHORIZATION_REQUIRED", True)
    try:
        value = response.json()
    except ValueError:
        raise ConnectorFailure("OAUTH_RESPONSE_INVALID", True) from None
    if not isinstance(value, dict):
        raise ConnectorFailure("OAUTH_RESPONSE_INVALID", True)
    account = value.get("stripe_user_id") or value.get("account_id")
    if (
        value.get("scope") != "stripe_apps"
        or value.get("token_type") != "bearer"
        or value.get("livemode") != (mode == "live")
        or not isinstance(account, str)
        or not re.fullmatch(r"acct_[A-Za-z0-9]+", account)
    ):
        raise ConnectorFailure("OAUTH_IDENTITY_INVALID", True)
    if not all(
        isinstance(value.get(k), str) and 10 <= len(value[k]) <= 4096 for k in ["access_token", "refresh_token"]
    ):
        raise ConnectorFailure("OAUTH_TOKEN_INVALID", True)
    return {"access_token": value["access_token"], "refresh_token": value["refresh_token"], "account_id": account}


def oauth_start(store: Store, user_id: int, session_key: str) -> str:
    state = secrets.token_urlsafe(32)
    OAuthState.objects.create(
        organization_id=store.organization_id,
        state_hash=digest(state.encode()),
        user_id=user_id,
        session_hash=digest(session_key.encode()),
        store=store,
        expires_at=timezone.now() + timedelta(minutes=10),
        mode=store.mode,
    )
    client = settings.STRIPE_LIVE_CLIENT_ID if store.mode == "live" else settings.STRIPE_TEST_CLIENT_ID
    if not client:
        raise Problem("STRIPE_APP_NOT_CONFIGURED", 503)
    return "https://marketplace.stripe.com/oauth/v2/authorize?" + urlencode(
        {"client_id": client, "redirect_uri": settings.PUBLIC_URL + "/oauth/stripe/callback", "state": state}
    )


def oauth_callback(state: str, code: str, user_id: int, session_key: str) -> Installation:
    row = (
        OAuthState.objects.select_for_update()
        .filter(
            state_hash=digest(state.encode()),
            user_id=user_id,
            session_hash=digest(session_key.encode()),
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if row is None:
        raise Problem("INVALID_OAUTH_STATE", 400)
    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at"])
    tokens = exchange(row.mode, {"grant_type": "authorization_code", "code": code})
    with transaction.atomic():
        connector = Installation.objects.filter(
            organization_id=row.organization_id,
            kind="stripe",
            account_id=tokens["account_id"],
            mode=row.mode,
            status="active",
        ).first()
        if connector is None:
            connector = Installation.objects.create(
                organization_id=row.organization_id,
                kind="stripe",
                mode=row.mode,
                account_id=tokens["account_id"],
                destination_id=UUID(bytes=secrets.token_bytes(16), version=4),
            )
            ResourceLocator.objects.create(id=connector.id, organization_id=row.organization_id, kind="connector")
            ResourceLocator.objects.create(
                id=str(connector.destination_id), organization_id=row.organization_id, kind="destination"
            )
        connector.credential_ciphertext = encrypt_secret(tokens, str(row.organization_id), str(connector.id), "oauth")
        connector.token_expires_at = timezone.now() + timedelta(minutes=55)
        connector.status = "active"
        connector.lock_version += 1
        connector.save()
        StoreStripeLink.objects.update_or_create(
            store=row.store,
            defaults={
                "organization_id": row.organization_id,
                "stripe": connector,
                "status": "unverified",
                "owner_confirmed_at": timezone.now(),
                "merchant_confirmed_at": Installation.objects.filter(store=row.store, kind="woo", status="active")
                .values_list("created_at", flat=True)
                .first(),
                "verified_payment_id": "",
            },
        )
        audit(row.organization_id, str(user_id), "stripe.connected", connector.id)
        from modules.ingestion.service import enqueue

        enqueue(
            row.organization_id,
            "stripe_repair",
            connector.id,
            f"initial-stripe:{connector.id}:{connector.lock_version}",
        )
        return connector


def refresh_access_token(connector: Installation) -> str:
    """Caller must hold SELECT FOR UPDATE on Installation for the whole provider operation."""
    if connector.status != "active":
        raise ConnectorFailure("CONNECTOR_INACTIVE", True)
    tokens = decrypt_secret(connector.credential_ciphertext, str(connector.organization_id), str(connector.id), "oauth")
    if connector.token_expires_at is None or connector.token_expires_at <= timezone.now():
        tokens = exchange(connector.mode, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        if tokens["account_id"] != connector.account_id:
            raise ConnectorFailure("OAUTH_ACCOUNT_CHANGED", True)
        connector.credential_ciphertext = encrypt_secret(
            tokens, str(connector.organization_id), str(connector.id), "oauth"
        )
        connector.token_expires_at = timezone.now() + timedelta(minutes=55)
        connector.lock_version += 1
        connector.save()
        audit(connector.organization_id, "worker", "stripe.tokens_rotated", connector.id)
    return str(tokens["access_token"])


def access_token(connector: Installation) -> str:
    """Resource reads must never rotate a credential inside their rollback boundary."""
    if connector.status != "active":
        raise ConnectorFailure("CONNECTOR_INACTIVE", True)
    if connector.token_expires_at is None or connector.token_expires_at <= timezone.now():
        raise ConnectorFailure("OAUTH_REFRESH_REQUIRED")
    tokens = decrypt_secret(connector.credential_ciphertext, str(connector.organization_id), str(connector.id), "oauth")
    return str(tokens["access_token"])


class StripeReader:
    def __init__(self, connector: Installation) -> None:
        self.connector = connector
        self.token = access_token(connector)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not RESOURCE_PATH.fullmatch(path):
            raise ValueError("Unsupported read resource")
        # Five requests per second across this connector; the database lock serializes callers.
        if self.connector.next_api_at:
            delay = (self.connector.next_api_at - timezone.now()).total_seconds()
            if delay > 0:
                time.sleep(min(delay, 1))
        self.connector.next_api_at = timezone.now() + timedelta(milliseconds=200)
        self.connector.save(update_fields=["next_api_at"])
        try:
            response = httpx.get(
                "https://api.stripe.com" + path,
                params=params,
                auth=(self.token, ""),
                headers={"Stripe-Version": settings.STRIPE_API_VERSION},
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ConnectorFailure("STRIPE_NETWORK_FAILURE") from None
        if response.status_code in {401, 403}:
            raise ConnectorFailure("STRIPE_AUTHORIZATION_REVOKED", True)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "1")
            raise ConnectorFailure("STRIPE_RATE_LIMITED", retry_after=min(int(retry), 300) if retry.isdigit() else 5)
        if response.status_code >= 500:
            raise ConnectorFailure("STRIPE_UNAVAILABLE")
        if response.status_code != 200:
            raise ConnectorFailure("STRIPE_RESOURCE_UNAVAILABLE", response.status_code != 404)
        if len(response.content) > 8_388_608:
            raise ConnectorFailure("STRIPE_RESPONSE_TOO_LARGE")
        try:
            value = response.json()
        except ValueError:
            raise ConnectorFailure("STRIPE_SCHEMA_INVALID") from None
        if not isinstance(value, dict):
            raise ConnectorFailure("STRIPE_SCHEMA_INVALID")
        return value

    def pages(self, path: str, params: dict[str, Any]) -> Any:
        cursor = ""
        for _ in range(2000):
            page = self.get(path, {**params, "limit": 100, **({"starting_after": cursor} if cursor else {})})
            rows = page.get("data")
            if not isinstance(rows, list) or not isinstance(page.get("has_more"), bool):
                raise ConnectorFailure("STRIPE_PAGINATION_INVALID")
            yield rows
            if not page["has_more"]:
                return
            if not rows or rows[-1].get("id") == cursor:
                raise ConnectorFailure("STRIPE_PAGINATION_STALLED")
            cursor = rows[-1]["id"]
        raise ConnectorFailure("STRIPE_SCAN_LIMIT_EXCEEDED")


def _id(value: Any, optional: bool = True) -> str:
    if value is None and optional:
        return ""
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str) or not OBJECT_ID.fullmatch(value):
        raise ConnectorFailure("STRIPE_ID_INVALID")
    return value


def normalize(value: dict[str, Any], kind: str, connector: Installation) -> tuple[str, dict[str, Any]]:
    source_id = _id(value.get("id"), False)
    if value.get("livemode") != (connector.mode == "live"):
        raise ConnectorFailure("STRIPE_MODE_CONFLICT", True)
    amount = parse_stripe(value["amount_total"] if kind == "session" else value["amount"], value.get("currency", ""))
    created = value.get("created")
    if type(created) is not int or created < 0:
        raise ConnectorFailure("STRIPE_TIMESTAMP_INVALID")
    status = value.get("payment_status") if kind == "session" else value.get("status")
    if kind == "charge":
        status = "captured" if value.get("captured") is True else "uncaptured"
    elif status not in STATUSES[kind]:
        raise ConnectorFailure("STRIPE_STATUS_UNSUPPORTED")
    data = {
        "amount_minor": amount.minor,
        "currency": amount.currency,
        "exponent": amount.exponent,
        "status": status,
        "created_at_source": utc(datetime.fromtimestamp(created, UTC)),
    }
    if kind == "payment":
        metadata = value.get("metadata") or {}
        order_hint = metadata.get("order_id", "")
        store_hint = metadata.get("ledgerguard_store_id", "")
        if not isinstance(order_hint, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", order_hint):
            order_hint = ""
        try:
            store_hint = str(UUID(store_hint)) if store_hint else ""
        except (ValueError, TypeError):
            store_hint = ""
        types = value.get("payment_method_types") or []
        method = types[0] if len(types) == 1 and re.fullmatch(r"[a-z_]{1,64}", types[0]) else "unknown"
        data.update(
            received_minor=parse_stripe(value["amount_received"], amount.currency).minor,
            capturable_minor=parse_stripe(value["amount_capturable"], amount.currency).minor,
            charge_id=_id(value.get("latest_charge")),
            method=method,
            capture_method=value.get("capture_method", "automatic"),
            order_hint=order_hint,
            store_hint=store_hint,
        )
    elif kind == "charge":
        data.update(
            parent_id=_id(value.get("payment_intent")),
            refunded_minor=parse_stripe(value["amount_refunded"], amount.currency).minor,
        )
    else:
        data.update(
            parent_id=_id(value.get("payment_intent")),
            charge_id=_id(value.get("charge")) if kind == "stripe_refund" else "",
        )
    return source_id, data


def fetch_resource(reader: StripeReader, object_kind: str, source_id: str) -> Projection | None:
    path = {
        "payment_intent": "payment_intents",
        "charge": "charges",
        "refund": "refunds",
        "checkout.session": "checkout/sessions",
    }[object_kind]
    expected_id = _id(source_id, False)
    if not expected_id.startswith(OBJECT_PREFIXES[object_kind]):
        raise ConnectorFailure("STRIPE_ID_INVALID", True)
    value = reader.get("/v1/" + path + "/" + expected_id)
    if _id(value.get("id"), False) != expected_id or value.get("object") != object_kind:
        raise ConnectorFailure("STRIPE_IDENTITY_CONFLICT", True)
    if value.get("livemode") != (reader.connector.mode == "live"):
        raise ConnectorFailure("STRIPE_MODE_CONFLICT", True)
    if object_kind == "checkout.session" and value.get("payment_intent") is None:
        # Subscription and zero-payment sessions are outside the PaymentIntent contract.
        return None
    kind = {"payment_intent": "payment", "charge": "charge", "refund": "stripe_refund", "checkout.session": "session"}[
        object_kind
    ]
    sid, data = normalize(value, kind, reader.connector)
    if kind == "stripe_refund" and not data["parent_id"] and data["charge_id"]:
        charge = reader.get("/v1/charges/" + data["charge_id"])
        data["parent_id"] = _id(charge.get("payment_intent"))
    if kind in {"charge", "stripe_refund", "session"} and not data["parent_id"]:
        # Legacy charges are outside the PaymentIntent contract.
        return None
    projection, _ = upsert(reader.connector, kind, sid, timezone.now(), data)
    if kind != "payment" and data["parent_id"]:
        fetch_resource(reader, "payment_intent", data["parent_id"])
    return projection


def process_receipt(receipt_id: UUID) -> None:
    receipt = Receipt.objects.select_for_update().get(id=receipt_id)
    if receipt.status in {"normalized", "ignored"}:
        return
    connector = Installation.objects.select_for_update().get(id=receipt.connector_id, status="active")
    reader = StripeReader(connector)
    fetch_resource(reader, receipt.kind, receipt.source_id)
    receipt.status = "normalized"
    receipt.save(update_fields=["status"])
    reconcile_later(connector, str(receipt.id))


def poll(connector_id: UUID, repair: bool = False) -> None:
    from modules.ingestion.models import Scan
    from modules.ingestion.service import enqueue

    connector = Installation.objects.select_for_update().get(id=connector_id, kind="stripe", status="active")
    if Scan.objects.filter(connector=connector, status="pending").exists():
        return
    now = timezone.now().replace(microsecond=0) - timedelta(seconds=30)
    cursor = Cursor.objects.filter(connector=connector, object_class="stripe_payments").first()
    start = (
        now - timedelta(days=30)
        if repair or not cursor or not cursor.covered_through
        else max(now - timedelta(days=30), cursor.covered_through - timedelta(minutes=30))
    )
    scan = Scan.objects.create(
        organization_id=connector.organization_id,
        connector=connector,
        window_from=start,
        window_through=now,
        repair=repair,
    )
    enqueue(connector.organization_id, "stripe_scan_page", scan.id, f"scan:{scan.id}:0")


def scan_page(scan_id: UUID) -> None:
    from modules.ingestion.models import Scan
    from modules.ingestion.service import enqueue

    scan = Scan.objects.select_for_update().get(id=scan_id)
    if scan.status != "pending":
        return
    connector = Installation.objects.select_for_update().get(id=scan.connector_id, status="active")
    reader = StripeReader(connector)
    phases = [
        ("events", None),
        ("payment_intents", "payment_intent"),
        ("refunds", "refund"),
        ("checkout/sessions", "checkout.session"),
    ]
    if scan.phase < len(phases):
        path, kind = phases[scan.phase]
        params: dict[str, Any] = {
            "created[gte]": int(scan.window_from.timestamp()),
            "created[lte]": int(scan.window_through.timestamp()),
            "limit": 100,
        }
        if scan.provider_cursor:
            params["starting_after"] = scan.provider_cursor
        page = reader.get("/v1/" + path, params)
        rows = page.get("data")
        if not isinstance(rows, list) or not isinstance(page.get("has_more"), bool):
            raise ConnectorFailure("STRIPE_PAGINATION_INVALID")
        for value in rows:
            obj = value.get("data", {}).get("object", {}) if kind is None else value
            object_kind = obj.get("object") if kind is None else kind
            if object_kind in {"payment_intent", "charge", "refund", "checkout.session"}:
                fetch_resource(reader, object_kind, obj["id"])
        if page["has_more"]:
            if not rows or rows[-1]["id"] == scan.provider_cursor:
                raise ConnectorFailure("STRIPE_PAGINATION_STALLED")
            scan.provider_cursor = rows[-1]["id"]
        else:
            scan.phase += 1
            scan.provider_cursor = ""
    else:
        linked = list(StoreStripeLink.objects.filter(stripe=connector).values_list("store_id", flat=True))
        orders = Projection.objects.filter(store_id__in=linked, kind="order").order_by("id")
        if scan.provider_cursor:
            orders = orders.filter(id__gt=scan.provider_cursor)
        batch = list(orders[:25])
        for order in batch:
            for ref in sorted(
                set(filter(None, [order.transaction_id, order.pi_id, order.charge_id, order.session_id]))
            ):
                kind = (
                    "payment_intent"
                    if ref.startswith("pi_")
                    else "charge"
                    if ref.startswith("ch_")
                    else "checkout.session"
                )
                try:
                    fetch_resource(reader, kind, ref)
                except ConnectorFailure as exc:
                    if exc.code != "STRIPE_RESOURCE_UNAVAILABLE":
                        raise
        if batch:
            scan.provider_cursor = str(batch[-1].id)
        else:
            advance_stripe_coverage(connector, scan.window_from, scan.window_through)
            scan.status = "complete"
            reconcile_later(connector, str(scan.id))
    scan.page_count += 1
    if scan.page_count > 10000:
        raise ConnectorFailure("STRIPE_SCAN_LIMIT_EXCEEDED")
    scan.updated_at = timezone.now()
    scan.save()
    if scan.status == "pending":
        enqueue(connector.organization_id, "stripe_scan_page", scan.id, f"scan:{scan.id}:{scan.page_count}")


def disconnect(connector: Installation, actor: str) -> None:
    connector.status = "disconnected"
    connector.credential_ciphertext = {}
    connector.webhook_ciphertext = {}
    connector.token_expires_at = None
    connector.save()
    Cursor.objects.filter(connector=connector).update(complete=False)
    audit(connector.organization_id, actor, "connector.disconnected", connector.id)
