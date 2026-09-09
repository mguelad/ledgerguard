"""Transactional notification intents, SES delivery and terminal feedback handling."""

import hashlib
from datetime import timedelta
from typing import Any
from uuid import UUID

import boto3
from django.conf import settings
from django.utils import timezone

from apps.control_plane.errors import Problem
from modules.accounts.models import AuditLog, Membership, Organization
from modules.findings.models import AlertRoute, EmailDelivery, Finding, FindingEvent
from modules.ingestion.service import enqueue, quota


def routes() -> Any:
    emails = Membership.objects.filter(active=True, user__is_active=True).exclude(role="merchant").values("user__email")
    return AlertRoute.objects.filter(active=True, verified_at__isnull=False, bounced_at__isnull=True, email__in=emails)


def owner_routes() -> Any:
    emails = Membership.objects.filter(active=True, role="owner", user__is_active=True).values("user__email")
    return AlertRoute.objects.filter(active=True, verified_at__isnull=False, bounced_at__isnull=True, email__in=emails)


SECURITY_MESSAGES = {
    "alert_route.updated": ("Alert recipient changed", "An alert recipient setting was changed."),
    "connector.disconnected": ("Connector disconnected", "A payment-data connector was disconnected."),
    "connector.suspended": ("Connector suspended", "A connector was suspended after a permanent provider error."),
    "member.changed": ("Member access changed", "An organization member's access was changed."),
    "member.invited": ("Member invited", "A new organization invitation was created."),
    "member.joined": ("Member joined", "An invited member joined the organization."),
    "organization.deletion_requested": (
        "Organization deletion requested",
        "Organization deletion was requested and connector intake was disabled.",
    ),
    "report.requested": ("Evidence export requested", "An evidence report export was requested."),
    "stripe.connected": ("Stripe connected", "A Stripe account authorization was connected."),
    "stripe.webhook_secret_updated": (
        "Stripe webhook secret changed",
        "The Stripe webhook verification secret was changed.",
    ),
    "support.approved": ("Support access approved", "Time-limited support access was approved."),
    "support.revoked": ("Support access revoked", "Time-limited support access was revoked."),
    "woo.key_rotated": ("WooCommerce key rotated", "The WooCommerce connector signing key was rotated."),
    "woo.paired": ("WooCommerce paired", "A WooCommerce connector was paired."),
}


def prepare(event_id: UUID) -> None:
    event = FindingEvent.objects.select_related("finding__store").get(id=event_id)
    if not event.finding.store.alerts_enabled or not Organization.objects.get(id=event.organization_id).alerts_enabled:
        return
    if event.finding.state in {"resolved", "suppressed", "provisional"}:
        return
    try:
        quota(event.organization_id, "notification:" + event.finding.rule_code, 20, 3600)
    except Problem:
        hour = int(timezone.now().timestamp()) // 3600
        outbox = enqueue(
            event.organization_id, "notify_digest", event.organization_id, f"digest:{event.organization_id}:{hour}"
        )
        outbox.available_at = timezone.now() + timedelta(hours=1)
        outbox.save(update_fields=["available_at"])
        return
    for route in routes():
        delivery, _ = EmailDelivery.objects.get_or_create(
            event=event, route=route, defaults={"organization_id": event.organization_id}
        )
        enqueue(event.organization_id, "deliver_email", delivery.id, f"email:{delivery.id}")


def security(event_id: UUID) -> None:
    event = AuditLog.objects.get(id=event_id)
    if event.action not in SECURITY_MESSAGES:
        return
    for route in owner_routes():
        delivery, _ = EmailDelivery.objects.get_or_create(
            security_event=event,
            route=route,
            defaults={"organization_id": event.organization_id},
        )
        enqueue(event.organization_id, "deliver_email", delivery.id, f"email:{delivery.id}")


def deliver(delivery_id: UUID) -> None:
    delivery = (
        EmailDelivery.objects.select_for_update(of=("self",))
        .select_related("event__finding__store", "security_event", "route")
        .get(id=delivery_id)
    )
    if delivery.status in {"accepted", "delivered", "bounced", "complained", "local", "cancelled"}:
        return
    finding = delivery.event.finding if delivery.event else None
    if finding:
        allowed = (
            Organization.objects.filter(id=delivery.organization_id, active=True, alerts_enabled=True).exists()
            and routes().filter(id=delivery.route_id).exists()
            and finding.store.alerts_enabled
            and finding.state not in {"suppressed", "resolved", "provisional"}
        )
        subject = f"[{finding.severity.title()}] Payment integrity issue on {finding.store.hostname}"
        body = f"A payment integrity finding needs review.\n\nRule: {finding.rule_code}\nStore: {finding.store.name} ({finding.mode})\n\nReview the evidence and diagnostic steps:\n{settings.PUBLIC_URL}/findings/{finding.id}/\n\nLedgerGuard does not modify orders or payments."
    elif delivery.security_event:
        event = delivery.security_event
        message = SECURITY_MESSAGES.get(event.action)
        organization = Organization.objects.filter(id=delivery.organization_id).only("active").first()
        allowed = (
            message is not None
            and owner_routes().filter(id=delivery.route_id).exists()
            and organization is not None
            and (event.action == "organization.deletion_requested" or organization.active)
        )
        subject, description = message or ("Security change", "A security-sensitive change was recorded.")
        review_url = (
            settings.PUBLIC_URL + "/login/"
            if event.action == "organization.deletion_requested"
            else f"{settings.PUBLIC_URL}/o/{delivery.organization_id}/settings/"
        )
        body = (
            f"{description}\n\n"
            f"Review organization activity and access controls:\n{review_url}\n\n"
            "If this change was unexpected, revoke affected access and follow the security incident runbook."
        )
    else:
        count = Finding.objects.filter(state__in=["open", "reopened"], store__alerts_enabled=True).count()
        allowed = (
            Organization.objects.filter(id=delivery.organization_id, active=True, alerts_enabled=True).exists()
            and routes().filter(id=delivery.route_id).exists()
            and bool(count)
            and bool(delivery.digest_key)
        )
        subject = "Payment integrity findings require review"
        body = (
            f"{count} findings remain open. Review your dashboard: {settings.PUBLIC_URL}/o/{delivery.organization_id}/"
        )
    if not allowed:
        delivery.status = "cancelled"
        delivery.save()
        return
    if not settings.SES_FROM_EMAIL:
        if settings.PRODUCTION:
            raise Problem("EMAIL_NOT_CONFIGURED", 503)
        delivery.status = "local"
        delivery.save()
        return
    args: dict[str, Any] = {
        "Source": settings.SES_FROM_EMAIL,
        "Destination": {"ToAddresses": [delivery.route.email]},
        "Message": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
        "Tags": [
            {"Name": "organization", "Value": str(delivery.organization_id)},
            {"Name": "delivery", "Value": str(delivery.id)},
        ],
    }
    if settings.SES_CONFIGURATION_SET:
        args["ConfigurationSetName"] = settings.SES_CONFIGURATION_SET
    response = boto3.client("ses", region_name=settings.AWS_REGION).send_email(**args)
    delivery.status = "accepted"
    delivery.provider_id = response["MessageId"]
    delivery.accepted_at = timezone.now()
    delivery.save()


def digest(org: UUID, intent_key: str) -> None:
    if not Organization.objects.filter(id=org, active=True, alerts_enabled=True).exists():
        return
    for route in routes():
        key = hashlib.sha256(f"{intent_key}:{route.id}".encode()).hexdigest()
        delivery, _ = EmailDelivery.objects.get_or_create(
            digest_key=key, defaults={"organization_id": org, "route": route}
        )
        enqueue(org, "deliver_email", delivery.id, f"email:{delivery.id}")


def feedback(value: dict[str, Any]) -> None:
    """Invoked only from the SES feedback queue restricted to the configured SNS topic."""
    from modules.accounts.tenancy import tenant_scope

    mail = value.get("mail", {})
    tags = mail.get("tags", {})
    org, delivery_id = UUID(tags["organization"][0]), UUID(tags["delivery"][0])
    with tenant_scope(org):
        row = (
            EmailDelivery.objects.select_for_update()
            .select_related("route")
            .get(id=delivery_id, provider_id=mail["messageId"])
        )
        status = {"Bounce": "bounced", "Complaint": "complained", "Delivery": "delivered", "Reject": "bounced"}.get(
            str(value.get("eventType", ""))
        )
        if status and (row.status not in {"bounced", "complained"} or status == "complained"):
            row.status = status
            row.feedback_at = timezone.now()
            row.save()
            if status in {"bounced", "complained"}:
                row.route.bounced_at = timezone.now()
                row.route.active = False
                row.route.save()
