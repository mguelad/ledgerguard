import base64
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any
from uuid import UUID

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.control_plane.errors import Problem, problem_response
from modules.accounts.models import Organization
from modules.accounts.tenancy import audit, tenant_scope
from modules.connectors.crypto import decrypt_secret, verify_stripe, verify_woo
from modules.connectors.models import (
    Installation,
    PairingCode,
    PairingLocator,
    PublicKey,
    ResourceLocator,
    StoreStripeLink,
)
from modules.ingestion.models import Receipt
from modules.ingestion.service import accept_woo_batch, enqueue, quota
from modules.ingestion.validation import digest, parse_body, request_body, utc, validate


def machine_errors(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            return view(request, *args, **kwargs)
        except Problem as error:
            return problem_response(request, error)
        except (ValueError, KeyError, TypeError):
            return problem_response(request, Problem("INVALID_ENVELOPE", 422))

    return wrapped


def _active(connector: Installation) -> None:
    if (
        connector.status != "active"
        or not Organization.objects.filter(id=connector.organization_id, active=True).exists()
    ):
        raise Problem("CONNECTOR_INACTIVE", 403)


@csrf_exempt
@require_POST
@machine_errors
def claim(request: HttpRequest) -> JsonResponse:
    body = request_body(request)
    envelope = parse_body(body)
    validate(envelope, "woo-claim-1.0.json")
    code_hash = digest(envelope["pairing_code"].encode())
    route = PairingLocator.objects.filter(code_hash=code_hash).first()
    if route is None:
        raise Problem("PAIRING_CODE_INVALID", 401)
    with tenant_scope(route.organization_id):
        pairing = (
            PairingCode.objects.select_for_update()
            .select_related("store")
            .filter(code_hash=code_hash, expires_at__gt=timezone.now())
            .first()
        )
        if pairing is None or pairing.store.mode != envelope["mode"]:
            raise Problem("PAIRING_CODE_INVALID", 401)
        quota(route.organization_id, "pairing", 20)
        public = base64.b64decode(envelope["public_key"], validate=True)
        verify_woo(
            public,
            request.headers.get("X-LedgerGuard-Signature", ""),
            "claim",
            envelope["request_id"],
            envelope["sent_at"],
            body,
            timezone.now(),
        )
        existing = PublicKey.objects.filter(key_id=envelope["key_id"]).select_related("installation").first()
        if pairing.used_at:
            if (
                existing
                and existing.installation.store_id == pairing.store_id
                and bytes(existing.public_key) == public
                and existing.installation.status == "active"
            ):
                original = Receipt.objects.filter(
                    connector=existing.installation, external_id=envelope["request_id"], body_hash=digest(body)
                ).first()
                if not original:
                    original = Receipt.objects.filter(connector=existing.installation, kind="claim").first()
                if original:
                    return JsonResponse({**original.result, "server_time": utc(timezone.now())})
            raise Problem("PAIRING_CODE_USED", 409)
        if not Organization.objects.filter(id=route.organization_id, active=True).exists():
            raise Problem("ORGANIZATION_INACTIVE", 403)
        # Re-pair closes the old installation immediately; its evidence stays intact.
        Installation.objects.filter(store=pairing.store, kind="woo", status="active").update(status="disconnected")
        connector = Installation.objects.create(
            organization_id=route.organization_id,
            store=pairing.store,
            kind="woo",
            mode=pairing.store.mode,
            versions=envelope["versions"],
        )
        PublicKey.objects.create(
            organization_id=route.organization_id, installation=connector, key_id=envelope["key_id"], public_key=public
        )
        ResourceLocator.objects.create(id=connector.id, organization_id=route.organization_id, kind="connector")
        pairing.used_at = timezone.now()
        pairing.save(update_fields=["used_at"])
        StoreStripeLink.objects.filter(store=pairing.store).update(merchant_confirmed_at=timezone.now())
        result = {
            "installation_id": str(connector.id),
            "key_id": envelope["key_id"],
            "mode": connector.mode,
            "server_time": utc(timezone.now()),
            "schema_version": "1.0",
        }
        Receipt.objects.create(
            organization_id=route.organization_id,
            connector=connector,
            external_id=envelope["request_id"],
            body_hash=digest(body),
            kind="claim",
            status="normalized",
            result=result,
        )
        audit(route.organization_id, "woo_admin", "woo.paired", connector.id, {"public_key_id": envelope["key_id"]})
        return JsonResponse(result, status=201)


def signed_plugin(request: HttpRequest, kind: str) -> JsonResponse:
    body = request_body(request)
    envelope = parse_body(body)
    validate(envelope, f"woo-{kind}-1.0.json")
    route = ResourceLocator.objects.filter(id=envelope["installation_id"], kind="connector").first()
    if route is None:
        raise Problem("INSTALLATION_UNKNOWN", 401)
    with tenant_scope(route.organization_id):
        connector = Installation.objects.select_for_update().get(id=route.id, kind="woo")
        _active(connector)
        key = PublicKey.objects.filter(
            installation=connector, key_id=request.headers.get("X-LedgerGuard-Key-ID", ""), revoked_at__isnull=True
        ).first()
        if key is None or (key.expires_at and key.expires_at <= timezone.now()):
            raise Problem("KEY_INACTIVE", 401)
        # Timestamp is checked against server UTC. A heartbeat cannot grant itself extra skew.
        verify_woo(
            bytes(key.public_key),
            request.headers.get("X-LedgerGuard-Signature", ""),
            envelope["installation_id"],
            envelope["request_id"],
            envelope["sent_at"],
            body,
            timezone.now(),
        )
        existing = Receipt.objects.filter(connector=connector, external_id=envelope["request_id"]).first()
        if existing:
            if existing.body_hash != digest(body):
                raise Problem("REQUEST_ID_CONFLICT", 409)
            return JsonResponse(existing.result)
        quota(connector.organization_id, "plugin:" + str(connector.id), 240)
        if kind == "batch":
            result = accept_woo_batch(connector, envelope)
        elif kind == "heartbeat":
            connector.last_heartbeat_at = timezone.now()
            connector.versions = envelope["versions"]
            connector.health = {k: envelope[k] for k in ["queue_depth", "hpos", "clock_offset_seconds"]}
            connector.health["clock_offset_seconds"] = round(
                (datetime.fromisoformat(envelope["sent_at"].replace("Z", "+00:00")) - timezone.now()).total_seconds()
            )
            connector.save()
            result = {"request_id": envelope["request_id"], "server_time": utc(timezone.now()), "status": "accepted"}
        else:
            existing_key = PublicKey.objects.filter(key_id=envelope["new_key_id"]).first()
            if existing_key and (
                existing_key.installation_id != connector.id
                or bytes(existing_key.public_key) != base64.b64decode(envelope["new_public_key"], validate=True)
                or existing_key.revoked_at
            ):
                raise Problem("KEY_ID_CONFLICT", 409)
            if not existing_key:
                PublicKey.objects.create(
                    organization_id=connector.organization_id,
                    installation=connector,
                    key_id=envelope["new_key_id"],
                    public_key=base64.b64decode(envelope["new_public_key"], validate=True),
                )
            if key.expires_at is None:
                key.expires_at = timezone.now() + timedelta(hours=24)
                key.save(update_fields=["expires_at"])
            audit(connector.organization_id, "woo_admin", "woo.key_rotated", connector.id)
            result = {"request_id": envelope["request_id"], "status": "accepted", "server_time": utc(timezone.now())}
        Receipt.objects.create(
            organization_id=connector.organization_id,
            connector=connector,
            external_id=envelope["request_id"],
            body_hash=digest(body),
            kind=kind,
            status="normalized",
            result=result,
        )
        return JsonResponse(result)


@csrf_exempt
@require_POST
@machine_errors
def batch(request: HttpRequest) -> JsonResponse:
    return signed_plugin(request, "batch")


@csrf_exempt
@require_POST
@machine_errors
def heartbeat(request: HttpRequest) -> JsonResponse:
    return signed_plugin(request, "heartbeat")


@csrf_exempt
@require_POST
@machine_errors
def rotate(request: HttpRequest) -> JsonResponse:
    return signed_plugin(request, "rotation")


@csrf_exempt
@require_POST
@machine_errors
def stripe_webhook(request: HttpRequest, destination_id: UUID) -> JsonResponse:
    body = request_body(request)
    route = ResourceLocator.objects.filter(id=destination_id, kind="destination").first()
    if route is None:
        raise Problem("DESTINATION_UNKNOWN", 401)
    with tenant_scope(route.organization_id):
        connector = Installation.objects.select_for_update().get(destination_id=destination_id)
        _active(connector)
        stored = decrypt_secret(
            connector.webhook_ciphertext, str(connector.organization_id), str(connector.id), "webhook"
        )
        secrets_to_check = list(stored["secrets"])
        previous = stored.get("previous")
        if previous and datetime.fromisoformat(previous["expires_at"]) > timezone.now():
            secrets_to_check.append(previous["value"])
        verify_stripe(body, request.headers.get("Stripe-Signature", ""), secrets_to_check, timezone.now())
        value = parse_body(body)
        if (
            value.get("livemode") != (connector.mode == "live")
            or value.get("account", connector.account_id) != connector.account_id
        ):
            raise Problem("STRIPE_NAMESPACE_CONFLICT", 403)
        from modules.connectors.stripe import OBJECT_ID, OBJECT_PREFIXES

        event_id = value.get("id", "")
        if not isinstance(event_id, str) or not event_id.startswith("evt_") or not OBJECT_ID.fullmatch(event_id):
            raise Problem("STRIPE_EVENT_INVALID", 422)
        existing = Receipt.objects.filter(connector=connector, external_id=event_id).first()
        if existing:
            if existing.body_hash != digest(body):
                raise Problem("STRIPE_EVENT_ID_CONFLICT", 409)
            return JsonResponse({"receipt_id": str(existing.id), "status": "duplicate"})
        quota(connector.organization_id, "stripe:" + str(connector.id), 2400)
        event_data = value.get("data")
        if not isinstance(event_data, dict) or not isinstance(event_data.get("object"), dict):
            raise Problem("STRIPE_EVENT_INVALID", 422)
        obj = event_data["object"]
        kind = obj.get("object")
        supported = kind in {"payment_intent", "charge", "refund", "checkout.session"}
        source_id = obj.get("id", "") if supported else ""
        if supported and (
            not isinstance(source_id, str)
            or not OBJECT_ID.fullmatch(source_id)
            or not source_id.startswith(OBJECT_PREFIXES[kind])
        ):
            raise Problem("STRIPE_OBJECT_INVALID", 422)
        event_type = value.get("type", "")
        if not isinstance(event_type, str) or not re.fullmatch(r"[a-z0-9_.]{1,100}", event_type):
            raise Problem("STRIPE_EVENT_INVALID", 422)
        created = value.get("created")
        if type(created) is not int or not 0 <= created <= int((timezone.now() + timedelta(minutes=5)).timestamp()):
            raise Problem("STRIPE_EVENT_INVALID", 422)
        receipt = Receipt.objects.create(
            organization_id=connector.organization_id,
            connector=connector,
            external_id=event_id,
            body_hash=digest(body),
            kind=kind if supported else "unsupported",
            source_id=source_id,
            event_type=event_type,
            event_created_at=datetime.fromtimestamp(created, UTC),
            status="pending" if supported else "ignored",
        )
        if supported:
            enqueue(connector.organization_id, "stripe_receipt", receipt.id, f"stripe-receipt:{receipt.id}")
        return JsonResponse({"receipt_id": str(receipt.id), "status": "accepted"})
