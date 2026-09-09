from django.db import models

from modules.base import TenantModel


class ResourceLocator(models.Model):
    """Opaque route to tenant mapping. Never credentials or source data."""

    id = models.UUIDField(primary_key=True)
    organization_id = models.UUIDField(db_index=True)
    kind = models.CharField(max_length=20)


class Store(TenantModel):
    name = models.CharField(max_length=100)
    hostname = models.CharField(max_length=253)
    mode = models.CharField(max_length=4, choices=[("test", "Test"), ("live", "Live")])
    active = models.BooleanField(default=True)
    policy_version = models.PositiveIntegerField(default=1)
    instant_grace_minutes = models.PositiveSmallIntegerField(default=10)
    async_grace_minutes = models.PositiveSmallIntegerField(default=30)
    refund_grace_minutes = models.PositiveSmallIntegerField(default=60)
    freshness_minutes = models.PositiveSmallIntegerField(default=15)
    disabled_rules = models.JSONField(default=list)
    shadow_rules = models.BooleanField(default=False)
    alerts_enabled = models.BooleanField(default=False)
    dry_run_reviewed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization_id", "hostname", "mode"], name="store_hostname_mode_uniq")
        ]


class Installation(TenantModel):
    kind = models.CharField(max_length=10, choices=[("woo", "Woo"), ("stripe", "Stripe")])
    store = models.ForeignKey(Store, null=True, on_delete=models.PROTECT)
    mode = models.CharField(max_length=4)
    account_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, default="active")
    destination_id = models.UUIDField(null=True, unique=True)
    credential_ciphertext = models.JSONField(default=dict)
    webhook_ciphertext = models.JSONField(default=dict)
    token_expires_at = models.DateTimeField(null=True)
    last_heartbeat_at = models.DateTimeField(null=True)
    versions = models.JSONField(default=dict)
    health = models.JSONField(default=dict)
    last_error_code = models.CharField(max_length=64, blank=True)
    next_api_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "account_id", "mode"],
                condition=models.Q(kind="stripe", status="active"),
                name="stripe_active_account_mode_uniq",
            )
        ]


class StoreStripeLink(TenantModel):
    store = models.OneToOneField(Store, on_delete=models.CASCADE)
    stripe = models.ForeignKey(Installation, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, default="unverified")
    owner_confirmed_at = models.DateTimeField(null=True)
    merchant_confirmed_at = models.DateTimeField(null=True)
    verified_payment_id = models.CharField(max_length=255, blank=True)


class PublicKey(TenantModel):
    installation = models.ForeignKey(Installation, on_delete=models.CASCADE)
    key_id = models.UUIDField(unique=True)
    public_key = models.BinaryField(max_length=32)
    expires_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)


class PairingCode(TenantModel):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    code_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)
    issued_by = models.CharField(max_length=64)


class PairingLocator(models.Model):
    code_hash = models.CharField(max_length=64, primary_key=True)
    organization_id = models.UUIDField()


class OAuthState(TenantModel):
    state_hash = models.CharField(max_length=64, unique=True)
    user_id = models.PositiveBigIntegerField()
    session_hash = models.CharField(max_length=64)
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True)
    mode = models.CharField(max_length=4)
