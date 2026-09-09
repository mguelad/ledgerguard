from django.db import models
from django.utils import timezone

from modules.base import ImmutableTenantModel, TenantModel
from modules.connectors.models import Store


class ReconciliationRun(ImmutableTenantModel):
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    mode = models.CharField(max_length=4)
    rule_version = models.CharField(max_length=32)
    match_version = models.CharField(max_length=32)
    policy_version = models.PositiveIntegerField()
    input_digest = models.CharField(max_length=64)
    evaluated_at = models.DateTimeField()
    coverage = models.JSONField()
    warnings = models.JSONField(default=list)
    proposals = models.JSONField(default=list)
    shadow = models.BooleanField(default=False)


class Finding(TenantModel):
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    mode = models.CharField(max_length=4)
    finding_key = models.CharField(max_length=64, unique=True)
    rule_code = models.CharField(max_length=6)
    rule_version = models.CharField(max_length=32)
    policy_version = models.PositiveIntegerField()
    severity = models.CharField(max_length=16)
    state = models.CharField(max_length=20, default="provisional")
    identities = models.JSONField(default=list)
    risk_amount_minor = models.BigIntegerField(null=True)
    currency = models.CharField(max_length=3, blank=True)
    exponent = models.PositiveSmallIntegerField(default=2)
    match_confidence = models.PositiveSmallIntegerField()
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    eligible_at = models.DateTimeField()
    occurrence_count = models.PositiveIntegerField(default=0)
    evidence = models.JSONField(default=dict)
    evidence_hash = models.CharField(max_length=64)
    suppressed_until = models.DateTimeField(null=True)
    clean_since = models.DateTimeField(null=True)
    clean_coverage_digest = models.CharField(max_length=64, blank=True)
    resolved_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["organization_id", "store", "state", "severity", "first_seen"])]


class FindingEvent(ImmutableTenantModel):
    finding = models.ForeignKey(Finding, on_delete=models.CASCADE)
    event_type = models.CharField(max_length=32)
    actor_id = models.CharField(max_length=64)
    previous_state = models.CharField(max_length=20)
    new_state = models.CharField(max_length=20)
    evidence = models.JSONField(default=dict)
    note = models.CharField(max_length=1000, blank=True)
    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, null=True)


class Outbox(TenantModel):
    dedupe_key = models.CharField(max_length=255, unique=True)
    task_type = models.CharField(max_length=32)
    resource_id = models.UUIDField()
    status = models.CharField(max_length=16, default="pending")
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True)
    provider_id = models.CharField(max_length=255, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    delivery_state = models.CharField(max_length=16, blank=True)

    class Meta:
        indexes = [models.Index(fields=["organization_id", "status", "available_at"])]


class AlertRoute(TenantModel):
    email = models.EmailField()
    verified_at = models.DateTimeField(null=True)
    active = models.BooleanField(default=True)
    bounced_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization_id", "email"], name="alert_route_email_uniq")]


class EmailDelivery(TenantModel):
    digest_key = models.CharField(max_length=64, null=True, unique=True)
    event = models.ForeignKey(FindingEvent, on_delete=models.CASCADE, null=True)
    security_event = models.ForeignKey("accounts.AuditLog", on_delete=models.PROTECT, null=True)
    route = models.ForeignKey(AlertRoute, on_delete=models.CASCADE)
    status = models.CharField(max_length=16, default="pending")
    provider_id = models.CharField(max_length=255, blank=True)
    accepted_at = models.DateTimeField(null=True)
    feedback_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "route"], name="email_event_route_uniq"),
            models.UniqueConstraint(fields=["security_event", "route"], name="email_security_route_uniq"),
            models.CheckConstraint(
                condition=(
                    models.Q(event__isnull=False, security_event__isnull=True, digest_key__isnull=True)
                    | models.Q(event__isnull=True, security_event__isnull=False, digest_key__isnull=True)
                    | models.Q(event__isnull=True, security_event__isnull=True, digest_key__isnull=False)
                ),
                name="email_delivery_kind_one",
            ),
        ]
