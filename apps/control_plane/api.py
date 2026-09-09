import json
import re
import secrets
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from rest_framework import serializers

from apps.control_plane.errors import Problem, problem_response
from modules.accounts.models import Invitation, Membership, Organization, SupportGrant, TenantDirectory
from modules.accounts.tenancy import audit, require_recent_auth, require_role, tenant_scope, tenant_view
from modules.base import uuid7
from modules.connectors import stripe
from modules.connectors.crypto import decrypt_secret, encrypt_secret
from modules.connectors.models import (
    Installation,
    PairingCode,
    PairingLocator,
    PublicKey,
    ResourceLocator,
    Store,
    StoreStripeLink,
)
from modules.findings.models import AlertRoute, Finding, ReconciliationRun
from modules.findings.service import action
from modules.ingestion.models import IdempotencyRecord, Projection
from modules.ingestion.service import enqueue, quota
from modules.ingestion.validation import digest, parse_body, request_body
from modules.reporting.models import Report
from modules.reporting.service import download, finding_row
from packages.reconciliation_core.domain import RULE_CODES
from packages.reconciliation_core.money import Money


def user_id(request: HttpRequest) -> int:
    if not request.user.is_authenticated or request.user.pk is None:
        raise Problem("AUTHENTICATION_REQUIRED", 401)
    return int(request.user.pk)


class StrictSerializer(serializers.Serializer[dict[str, Any]]):
    def to_internal_value(self, data: Any) -> Any:
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError("Unknown fields")
        return super().to_internal_value(data)


class StoreSerializer(StrictSerializer):
    name = serializers.CharField(max_length=100)
    hostname = serializers.RegexField(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
    mode = serializers.ChoiceField(choices=["test", "live"])


class ActionSerializer(StrictSerializer):
    note = serializers.CharField(max_length=1000, allow_blank=True, required=False, default="")
    until = serializers.DateTimeField(required=False)


def payload(
    request: HttpRequest, serializer: type[serializers.Serializer[Any]] | None = None, allowed: set[str] | None = None
) -> dict[str, Any]:
    data = parse_body(request_body(request))
    if serializer:
        value = serializer(data=data)
        if not value.is_valid():
            raise Problem("VALIDATION_FAILED", 422, "Check the required fields and allowed values.")
        return dict(value.validated_data)
    if allowed is not None and set(data) - allowed:
        raise Problem("UNKNOWN_FIELDS", 422)
    return data


def idempotent(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise Problem("IDEMPOTENCY_KEY_REQUIRED", 400)
        org = UUID(str(getattr(request, "organization_id", "")))
        principal = str(user_id(request))
        Organization.objects.select_for_update().get(id=org)
        body_hash = digest(request.body)
        existing = IdempotencyRecord.objects.filter(principal=principal, route=request.path, key=key).first()
        if existing and existing.expires_at > timezone.now():
            if existing.request_hash != body_hash:
                raise Problem("IDEMPOTENCY_KEY_CONFLICT", 409)
            value = existing.response
            if "sealed_response" in value:
                value = decrypt_secret(value["sealed_response"], str(org), str(existing.id), "idempotency")
            return JsonResponse(value, status=existing.response_status)
        if existing:
            existing.delete()
        response = view(request, *args, **kwargs)
        if isinstance(response, JsonResponse) and response.status_code < 400:
            result = json.loads(response.content)
            record = IdempotencyRecord(
                organization_id=org,
                principal=principal,
                route=request.path,
                key=key,
                request_hash=body_hash,
                response={},
                response_status=response.status_code,
                expires_at=timezone.now() + timedelta(hours=24),
            )
            record.response = (
                {"sealed_response": encrypt_secret(result, str(org), str(record.id), "idempotency")}
                if any(k in result for k in ["pairing_code", "invitation_token"])
                else result
            )
            record.save()
        return response

    return wrapped


def lock_version(request: HttpRequest) -> int:
    value = request.headers.get("If-Match", "").strip('"')
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise Problem("IF_MATCH_REQUIRED", 428)
    return int(value)


@require_POST
def create_organization(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return problem_response(request, Problem("AUTHENTICATION_REQUIRED", 401))
    try:
        require_recent_auth(request)
        data = payload(request, allowed={"name"})
        name = data.get("name")
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise Problem("IDEMPOTENCY_KEY_REQUIRED", 400)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise Problem("VALIDATION_FAILED", 422)
        body_hash = digest(request.body)
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(id=user_id(request))
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.user_id',%s,true)", [str(user_id(request))])
            memberships = list(Membership.objects.filter(user=request.user, active=True))
            for member in memberships:
                with tenant_scope(member.organization_id, user_id(request)):
                    prior = IdempotencyRecord.objects.filter(
                        principal=str(user_id(request)), route=request.path, key=key, expires_at__gt=timezone.now()
                    ).first()
                    if prior:
                        if prior.request_hash != body_hash:
                            raise Problem("IDEMPOTENCY_KEY_CONFLICT", 409)
                        request.session["organization_id"] = str(member.organization_id)
                        return JsonResponse(prior.response, status=prior.response_status)
            if len(memberships) >= 50:
                raise Problem("ORGANIZATION_LIMIT_REACHED", 422)
            org = uuid7()
            with tenant_scope(org, user_id(request)):
                TenantDirectory.objects.create(id=org)
                Organization.objects.create(id=org, organization_id=org, name=name.strip())
                Membership.objects.create(organization_id=org, user=request.user, role="owner")
                if request.user.email:
                    AlertRoute.objects.create(organization_id=org, email=request.user.email, verified_at=timezone.now())
                result = {"id": str(org), "location": f"/o/{org}/"}
                IdempotencyRecord.objects.create(
                    organization_id=org,
                    principal=str(user_id(request)),
                    route=request.path,
                    key=key,
                    request_hash=body_hash,
                    response=result,
                    response_status=201,
                    expires_at=timezone.now() + timedelta(hours=24),
                )
                audit(org, str(user_id(request)), "organization.created", org)
        request.session["organization_id"] = str(org)
        return JsonResponse(result, status=201)
    except Problem as error:
        return problem_response(request, error)


@require_POST
@tenant_view
@idempotent
def create_store(request: HttpRequest, organization_id: UUID) -> JsonResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    data = payload(request, StoreSerializer)
    if Store.objects.filter(hostname=data["hostname"], mode=data["mode"]).exists():
        raise Problem("STORE_ALREADY_EXISTS", 409)
    quota(organization_id, "store-create", 20, 3600)
    store = Store.objects.create(organization_id=organization_id, **data)
    ResourceLocator.objects.create(id=store.id, organization_id=organization_id, kind="store")
    audit(organization_id, str(user_id(request)), "store.created", store.id)
    return JsonResponse(
        {"id": str(store.id), "location": f"/stores/{store.id}/", "lock_version": store.lock_version}, status=201
    )


@require_POST
@tenant_view
@idempotent
def pairing_code(request: HttpRequest, store_id: UUID) -> JsonResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    payload(request, allowed=set())
    store = get_object_or_404(Store, id=store_id, active=True)
    token = secrets.token_urlsafe(32)
    expiry = timezone.now() + timedelta(minutes=10)
    PairingCode.objects.create(
        organization_id=store.organization_id,
        store=store,
        code_hash=digest(token.encode()),
        expires_at=expiry,
        issued_by=str(user_id(request)),
    )
    PairingLocator.objects.create(code_hash=digest(token.encode()), organization_id=store.organization_id)
    audit(store.organization_id, str(user_id(request)), "pairing_code.created", store.id)
    return JsonResponse({"pairing_code": token, "expires_at": expiry.isoformat(), "mode": store.mode}, status=201)


@require_GET
@tenant_view
def oauth_start(request: HttpRequest) -> HttpResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    try:
        store_id = UUID(request.GET.get("store_id", ""))
    except ValueError:
        raise Problem("STORE_REQUIRED", 422) from None
    store = get_object_or_404(Store, id=store_id)
    return redirect(stripe.oauth_start(store, user_id(request), request.session.session_key or ""))


@require_GET
@tenant_view
def oauth_callback(request: HttpRequest) -> HttpResponse:
    require_role(request, "owner", "admin")
    try:
        connector = stripe.oauth_callback(
            request.GET.get("state", ""),
            request.GET.get("code", ""),
            user_id(request),
            request.session.session_key or "",
        )
    except Problem as error:
        # Commit state consumption even when a provider exchange fails or its outcome is uncertain.
        return problem_response(request, error)
    return redirect(f"/o/{connector.organization_id}/")


@require_POST
@tenant_view
@idempotent
def verify_link(request: HttpRequest, store_id: UUID) -> JsonResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    data = payload(request, allowed={"order_id"})
    order = get_object_or_404(Projection, store_id=store_id, kind="order", source_id=str(data.get("order_id", "")))
    link = get_object_or_404(StoreStripeLink, store_id=store_id)
    if not link.owner_confirmed_at or not link.merchant_confirmed_at:
        raise Problem("BOTH_PARTIES_MUST_CONFIRM_LINK", 409)
    connector = Installation.objects.select_for_update().get(id=link.stripe_id, status="active")
    refs = set(filter(None, [order.transaction_id, order.pi_id, order.charge_id, order.session_id]))
    if not refs:
        raise Problem("EXACT_IDENTIFIER_REQUIRED", 422)
    reader = stripe.StripeReader(connector)
    matched = set()
    payment = None
    for ref in refs:
        kind = "payment_intent" if ref.startswith("pi_") else "charge" if ref.startswith("ch_") else "checkout.session"
        try:
            source = stripe.fetch_resource(reader, kind, ref)
        except stripe.ConnectorFailure as exc:
            if exc.code != "STRIPE_RESOURCE_UNAVAILABLE":
                raise
            source = None
        if source is None:
            link.status = "conflict"
            link.save()
            audit(link.organization_id, str(user_id(request)), "link.conflict", store_id)
            return JsonResponse(
                {
                    "status": "conflict",
                    "detail": "An exact identifier does not resolve in the selected account and mode.",
                },
                status=200,
            )
        pid = source.source_id if source.kind == "payment" else source.parent_id
        matched.add(pid)
        payment = Projection.objects.get(connector=connector, kind="payment", source_id=pid)
    agrees = payment is not None and Money(order.amount_minor, order.currency, order.exponent) == Money(
        payment.received_minor if payment.status == "succeeded" else payment.amount_minor,
        payment.currency,
        payment.exponent,
    )
    link.status = "verified" if len(matched) == 1 and agrees else "conflict"
    link.verified_payment_id = next(iter(matched)) if link.status == "verified" else ""
    link.save()
    audit(link.organization_id, str(user_id(request)), "link." + link.status, store_id)
    enqueue(link.organization_id, "reconcile_store", store_id, f"link:{link.id}:{uuid7()}")
    return JsonResponse({"status": link.status, "payment_id": link.verified_payment_id})


@require_POST
@tenant_view
@idempotent
def connector_disconnect(request: HttpRequest, connector_id: UUID) -> JsonResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    payload(request, allowed=set())
    connector = get_object_or_404(Installation.objects.select_for_update(), id=connector_id)
    if connector.lock_version != lock_version(request):
        raise Problem("VERSION_CONFLICT", 409)
    stripe.disconnect(connector, str(user_id(request)))
    PublicKey.objects.filter(installation=connector).update(revoked_at=timezone.now())
    return JsonResponse({"status": "disconnected"})


@require_POST
@tenant_view
@idempotent
def webhook_secret(request: HttpRequest, connector_id: UUID) -> JsonResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    data = payload(request, allowed={"signing_secret"})
    secret = data.get("signing_secret")
    if not isinstance(secret, str) or not re.fullmatch(r"whsec_[A-Za-z0-9]{16,200}", secret):
        raise Problem("INVALID_SIGNING_SECRET", 422)
    connector = get_object_or_404(
        Installation.objects.select_for_update(), id=connector_id, kind="stripe", status="active"
    )
    if connector.lock_version != lock_version(request):
        raise Problem("VERSION_CONFLICT", 409)
    values: dict[str, Any] = {"secrets": [secret]}
    if connector.webhook_ciphertext:
        previous = decrypt_secret(
            connector.webhook_ciphertext, str(connector.organization_id), str(connector.id), "webhook"
        )
        values["previous"] = {
            "value": previous["secrets"][0],
            "expires_at": (timezone.now() + timedelta(hours=24)).isoformat(),
        }
    connector.webhook_ciphertext = encrypt_secret(values, str(connector.organization_id), str(connector.id), "webhook")
    connector.lock_version += 1
    connector.save()
    audit(connector.organization_id, str(user_id(request)), "stripe.webhook_secret_updated", connector.id)
    return JsonResponse(
        {
            "destination_url": f"{settings.PUBLIC_URL}/webhooks/stripe/{connector.destination_id}",
            "lock_version": connector.lock_version,
        }
    )


@require_GET
@tenant_view
def findings(request: HttpRequest, store_id: UUID) -> JsonResponse:
    query = Finding.objects.filter(store_id=store_id).select_related("store").order_by("-id")
    if request.GET.get("state"):
        query = query.filter(state=request.GET["state"])
    if request.GET.get("rule"):
        query = query.filter(rule_code=request.GET["rule"])
    if request.GET.get("cursor"):
        try:
            cursor = signing.loads(request.GET["cursor"], salt="findings-cursor", max_age=3600)
            if cursor["store"] != str(store_id):
                raise ValueError("Scope")
            query = query.filter(id__lt=cursor["id"])
        except (signing.BadSignature, ValueError, KeyError):
            raise Problem("INVALID_CURSOR", 400) from None
    rows = list(query[:51])
    next_cursor = (
        signing.dumps({"id": str(rows[49].id), "store": str(store_id)}, salt="findings-cursor")
        if len(rows) > 50
        else None
    )
    return JsonResponse(
        {"results": [finding_row(f) | {"lock_version": f.lock_version} for f in rows[:50]], "next_cursor": next_cursor}
    )


@require_POST
@tenant_view
@idempotent
def finding_action(request: HttpRequest, finding_id: UUID, action_name: str) -> JsonResponse:
    require_role(request, "owner", "admin", "analyst")
    data = payload(request, ActionSerializer)
    finding = action(
        finding_id, str(user_id(request)), action_name, lock_version(request), data["note"], data.get("until")
    )
    return JsonResponse(
        {
            "id": str(finding.id),
            "state": finding.state,
            "lock_version": finding.lock_version,
            "location": f"/findings/{finding.id}/",
        }
    )


@require_GET
@tenant_view
def runs(request: HttpRequest) -> JsonResponse:
    rows = ReconciliationRun.objects.order_by("-created_at")[:50]
    return JsonResponse(
        {
            "results": [
                {
                    "id": str(r.id),
                    "store_id": str(r.store_id),
                    "rule_version": r.rule_version,
                    "policy_version": r.policy_version,
                    "input_digest": r.input_digest,
                    "evaluated_at": r.evaluated_at.isoformat(),
                    "coverage": r.coverage,
                    "warnings": r.warnings,
                    "shadow": r.shadow,
                }
                for r in rows
            ]
        }
    )


@require_POST
@tenant_view
@idempotent
def reports(request: HttpRequest) -> JsonResponse:
    require_role(request, "owner", "admin", "analyst")
    require_recent_auth(request)
    data = payload(request, allowed={"format", "store_id"})
    if data.get("format") not in {"csv", "pdf", "html"}:
        raise Problem("FORMAT_UNSUPPORTED", 422)
    store = get_object_or_404(Store, id=data["store_id"]) if data.get("store_id") else None
    org = UUID(str(getattr(request, "organization_id", "")))
    quota(org, "reports", 20, 3600)
    report = Report.objects.create(
        organization_id=org,
        requested_by=user_id(request),
        store=store,
        format=data["format"],
        expires_at=timezone.now() + timedelta(days=1),
    )
    ResourceLocator.objects.create(id=report.id, organization_id=org, kind="report")
    enqueue(org, "report", report.id, f"report:{report.id}")
    audit(org, str(user_id(request)), "report.requested", report.id)
    return JsonResponse({"id": str(report.id), "status": "pending", "location": f"/reports/{report.id}/"}, status=202)


@require_GET
@tenant_view
def report_download(request: HttpRequest, report_id: UUID) -> HttpResponse:
    report = get_object_or_404(Report, id=report_id)
    result = download(report)
    audit(report.organization_id, str(user_id(request)), "report.downloaded", report.id)
    if isinstance(result, str):
        return redirect(result)
    content, format = result
    response = HttpResponse(
        content, content_type={"csv": "text/csv", "html": "text/html", "pdf": "application/pdf"}[format]
    )
    response["Content-Disposition"] = f'attachment; filename="ledgerguard-{report.id}.{format}"'
    return response


@require_POST
@tenant_view
@idempotent
def policy(request: HttpRequest, store_id: UUID) -> JsonResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    data = payload(
        request,
        allowed={
            "instant_grace_minutes",
            "async_grace_minutes",
            "refund_grace_minutes",
            "freshness_minutes",
            "disabled_rules",
            "shadow_rules",
            "alerts_enabled",
            "review_dry_run",
        },
    )
    store = get_object_or_404(Store.objects.select_for_update(), id=store_id)
    if store.lock_version != lock_version(request):
        raise Problem("VERSION_CONFLICT", 409)
    for name, low, high in [
        ("instant_grace_minutes", 5, 60),
        ("async_grace_minutes", 10, 120),
        ("refund_grace_minutes", 30, 120),
        ("freshness_minutes", 5, 30),
    ]:
        if name in data:
            if type(data[name]) is not int or not low <= data[name] <= high:
                raise Problem("POLICY_OUT_OF_BOUNDS", 422)
            setattr(store, name, data[name])
    if "disabled_rules" in data:
        if not isinstance(data["disabled_rules"], list) or any(r not in RULE_CODES for r in data["disabled_rules"]):
            raise Problem("INVALID_RULE_CODE", 422)
        store.disabled_rules = sorted(set(data["disabled_rules"]))
    for field in ["shadow_rules", "alerts_enabled", "review_dry_run"]:
        if field in data and type(data[field]) is not bool:
            raise Problem("INVALID_BOOLEAN", 422)
    if data.get("review_dry_run"):
        if not ReconciliationRun.objects.filter(store=store).exists():
            raise Problem("DRY_RUN_REQUIRED", 409)
        store.dry_run_reviewed_at = timezone.now()
    if data.get("alerts_enabled"):
        last = ReconciliationRun.objects.filter(store=store).order_by("-created_at").first()
        if (
            not store.dry_run_reviewed_at
            or not last
            or any(p["rule"] == "PI-009" for p in last.proposals)
            or not StoreStripeLink.objects.filter(store=store, status="verified").exists()
        ):
            raise Problem("ONBOARDING_INCOMPLETE", 409)
    for field in ["shadow_rules", "alerts_enabled"]:
        if field in data:
            setattr(store, field, data[field])
    store.policy_version += 1
    store.lock_version += 1
    store.save()
    audit(store.organization_id, str(user_id(request)), "policy.updated", store.id, data)
    return JsonResponse({"policy_version": store.policy_version, "lock_version": store.lock_version})


@require_POST
@tenant_view
@idempotent
def invite_member(request: HttpRequest, organization_id: UUID) -> JsonResponse:
    require_role(request, "owner")
    require_recent_auth(request)
    data = payload(request, allowed={"email", "role", "store_id"})
    check = serializers.EmailField()
    try:
        email = check.run_validation(data.get("email"))
    except serializers.ValidationError:
        raise Problem("INVALID_EMAIL", 422) from None
    if data.get("role") not in {"admin", "analyst", "viewer", "merchant"}:
        raise Problem("INVALID_ROLE", 422)
    token = secrets.token_urlsafe(32)
    invite = Invitation.objects.create(
        organization_id=organization_id,
        email=email,
        role=data["role"],
        store_id=get_object_or_404(Store, id=data.get("store_id"), active=True).id
        if data["role"] == "merchant"
        else None,
        token_hash=digest(token.encode()),
        expires_at=timezone.now() + timedelta(days=2),
        invited_by_id=user_id(request),
    )
    PairingLocator.objects.create(code_hash=digest(token.encode()), organization_id=organization_id)
    audit(organization_id, str(user_id(request)), "member.invited", invite.id, {"role": data["role"]})
    return JsonResponse({"invitation_token": token, "expires_at": invite.expires_at.isoformat()}, status=201)


@require_POST
def accept_invitation(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return problem_response(request, Problem("AUTHENTICATION_REQUIRED", 401))
    try:
        data = payload(request, allowed={"token"})
        token = str(data.get("token", ""))
        locator = PairingLocator.objects.filter(code_hash=digest(token.encode())).first()
        if not locator:
            raise Problem("INVITATION_INVALID", 404)
        with tenant_scope(locator.organization_id, user_id(request)):
            invite = (
                Invitation.objects.select_for_update()
                .filter(
                    token_hash=digest(token.encode()),
                    email__iexact=request.user.email,
                    accepted_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
                .first()
            )
            if not invite:
                raise Problem("INVITATION_INVALID", 404)
            if Membership.objects.filter(user=request.user).exists():
                raise Problem("ALREADY_A_MEMBER", 409)
            Membership.objects.create(
                organization_id=invite.organization_id, user=request.user, role=invite.role, store_id=invite.store_id
            )
            invite.accepted_at = timezone.now()
            invite.save()
            audit(invite.organization_id, str(user_id(request)), "member.joined", invite.id)
        return JsonResponse(
            {
                "location": f"/stores/{invite.store_id}/"
                if invite.role == "merchant"
                else f"/o/{locator.organization_id}/"
            }
        )
    except Problem as error:
        return problem_response(request, error)


@require_POST
@tenant_view
@idempotent
def membership_change(request: HttpRequest, organization_id: UUID) -> JsonResponse:
    require_role(request, "owner")
    require_recent_auth(request)
    data = payload(request, allowed={"membership_id", "role", "active", "store_id"})
    member = get_object_or_404(Membership.objects.select_for_update(), id=data.get("membership_id"))
    if member.role == "owner":
        raise Problem("OWNERSHIP_TRANSFER_REQUIRES_REVIEW", 409)
    if data.get("role", member.role) not in {"admin", "analyst", "viewer", "merchant"}:
        raise Problem("INVALID_ROLE", 422)
    if "active" in data and type(data["active"]) is not bool:
        raise Problem("INVALID_BOOLEAN", 422)
    member.role = data.get("role", member.role)
    member.store_id = (
        get_object_or_404(Store, id=data.get("store_id") or member.store_id, active=True).id
        if member.role == "merchant"
        else None
    )
    member.active = data.get("active", member.active)
    member.lock_version += 1
    member.save()
    if not member.active or member.role == "merchant":
        AlertRoute.objects.filter(email=member.user.email).update(active=False)
    audit(
        organization_id,
        str(user_id(request)),
        "member.changed",
        member.id,
        {"role": member.role, "active": member.active},
    )
    return JsonResponse({"status": "updated"})


@require_POST
@tenant_view
@idempotent
def support_grant(request: HttpRequest, organization_id: UUID) -> JsonResponse:
    require_role(request, "owner")
    require_recent_auth(request)
    data = payload(request, allowed={"support_user_id", "reason", "minutes", "revoke_id"})
    if data.get("revoke_id"):
        grant = get_object_or_404(SupportGrant, id=data["revoke_id"])
        grant.revoked_at = timezone.now()
        grant.save()
        audit(organization_id, str(user_id(request)), "support.revoked", grant.id)
        return JsonResponse({"status": "revoked"})
    if (
        not isinstance(data.get("reason"), str)
        or not 10 <= len(data["reason"]) <= 500
        or type(data.get("support_user_id")) is not int
        or data["support_user_id"] < 1
        or type(data.get("minutes")) is not int
        or not 1 <= data["minutes"] <= 120
    ):
        raise Problem("SUPPORT_SCOPE_INVALID", 422)
    user = get_object_or_404(get_user_model(), id=data.get("support_user_id"), is_staff=True, is_active=True)
    grant = SupportGrant.objects.create(
        organization_id=organization_id,
        support_user=user,
        approved_by_id=user_id(request),
        reason=data["reason"],
        expires_at=timezone.now() + timedelta(minutes=data["minutes"]),
    )
    audit(
        organization_id,
        str(user_id(request)),
        "support.approved",
        grant.id,
        {"expires_at": grant.expires_at.isoformat()},
    )
    return JsonResponse({"id": str(grant.id), "expires_at": grant.expires_at.isoformat()}, status=201)


@require_POST
@tenant_view
@idempotent
def alert_route(request: HttpRequest, organization_id: UUID) -> JsonResponse:
    require_role(request, "owner", "admin")
    require_recent_auth(request)
    data = payload(request, allowed={"membership_id", "active"})
    member = get_object_or_404(Membership.objects.select_related("user"), id=data.get("membership_id"), active=True)
    if member.role == "merchant":
        raise Problem("ORGANIZATION_ALERTS_NOT_AVAILABLE_FOR_MERCHANT_ROLE", 422)
    if not member.user.email:
        raise Problem("VERIFIED_EMAIL_REQUIRED", 422)
    if type(data.get("active")) is not bool:
        raise Problem("INVALID_BOOLEAN", 422)
    route, _ = AlertRoute.objects.update_or_create(
        email=member.user.email,
        defaults={
            "organization_id": organization_id,
            "active": data["active"],
            "verified_at": timezone.now(),
            "bounced_at": None,
        },
    )
    audit(organization_id, str(user_id(request)), "alert_route.updated", route.id)
    return JsonResponse({"id": str(route.id), "active": route.active})


@require_POST
@tenant_view
@idempotent
def organization_delete(request: HttpRequest, organization_id: UUID) -> JsonResponse:
    require_role(request, "owner")
    require_recent_auth(request)
    data = payload(request, allowed={"confirm_name"})
    org = Organization.objects.select_for_update().get(id=organization_id)
    if data.get("confirm_name") != org.name:
        raise Problem("CONFIRMATION_NAME_MISMATCH", 422)
    audit(organization_id, str(user_id(request)), "organization.deletion_requested", organization_id)
    org.active = False
    org.deleted_at = timezone.now()
    org.save()
    Installation.objects.update(status="disconnected", credential_ciphertext={}, webhook_ciphertext={})
    PublicKey.objects.update(revoked_at=timezone.now())
    enqueue(organization_id, "delete_tenant", organization_id, f"delete:{organization_id}")
    return JsonResponse({"status": "deletion_pending"}, status=202)


@require_POST
@tenant_view
@idempotent
def organization_policy(request: HttpRequest, organization_id: UUID) -> JsonResponse:
    require_role(request, "owner")
    require_recent_auth(request)
    data = payload(request, allowed={"retention_days", "alerts_enabled"})
    org = Organization.objects.select_for_update().get(id=organization_id)
    if org.lock_version != lock_version(request):
        raise Problem("VERSION_CONFLICT", 409)
    if "retention_days" in data:
        if type(data["retention_days"]) is not int or data["retention_days"] not in {90, 180, 400}:
            raise Problem("RETENTION_OUT_OF_BOUNDS", 422)
        org.retention_days = data["retention_days"]
    if "alerts_enabled" in data:
        if type(data["alerts_enabled"]) is not bool:
            raise Problem("INVALID_BOOLEAN", 422)
        org.alerts_enabled = data["alerts_enabled"]
    org.lock_version += 1
    org.save(update_fields=["retention_days", "alerts_enabled", "lock_version", "updated_at"])
    audit(organization_id, str(user_id(request)), "organization.policy_updated", org.id, data)
    return JsonResponse(
        {"retention_days": org.retention_days, "alerts_enabled": org.alerts_enabled, "lock_version": org.lock_version}
    )
