from django.conf import settings
from django.db import models

from modules.base import ImmutableTenantModel, TenantModel, uuid7


class TenantDirectory(models.Model):
    """Routing/scheduling IDs only; contains no tenant business data."""

    id = models.UUIDField(primary_key=True, default=uuid7)
    active = models.BooleanField(default=True)
    erased_at = models.DateTimeField(null=True)
    erasure_digest = models.CharField(max_length=64, blank=True)


class Organization(TenantModel):
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    retention_days = models.PositiveSmallIntegerField(default=400)
    alerts_enabled = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(retention_days__in=[90, 180, 400]), name="org_retention_allowed")
        ]


class Membership(TenantModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(
        max_length=16, choices=[(r, r.title()) for r in ["owner", "admin", "analyst", "viewer", "merchant"]]
    )
    active = models.BooleanField(default=True)
    store_id = models.UUIDField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization_id", "user"], name="member_user_org_uniq")]


class Invitation(TenantModel):
    store_id = models.UUIDField(null=True)
    email = models.EmailField()
    role = models.CharField(max_length=16)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class SupportGrant(TenantModel):
    support_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=500)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)


class AuditLog(ImmutableTenantModel):
    actor_id = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    resource_id = models.CharField(max_length=64)
    correlation_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["organization_id", "created_at"])]
