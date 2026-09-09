from django.db import models

from modules.base import ImmutableTenantModel, TenantModel
from modules.connectors.models import Installation, Store


class Receipt(TenantModel):
    connector = models.ForeignKey(Installation, on_delete=models.PROTECT)
    external_id = models.CharField(max_length=255)
    body_hash = models.CharField(max_length=64)
    kind = models.CharField(max_length=32)
    source_id = models.CharField(max_length=255, blank=True)
    event_type = models.CharField(max_length=100, blank=True)
    event_created_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=20, default="pending")
    result = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connector", "external_id"], name="receipt_connector_external_uniq")
        ]


class Observation(ImmutableTenantModel):
    connector = models.ForeignKey(Installation, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    source_id = models.CharField(max_length=255)
    revision = models.DateTimeField()
    content_hash = models.CharField(max_length=64)
    data = models.JSONField()
    currency_table_version = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["connector", "kind", "source_id", "revision", "content_hash"], name="observation_revision_uniq"
            )
        ]
        indexes = [models.Index(fields=["organization_id", "connector", "kind", "source_id"])]


class Projection(TenantModel):
    """Typed shared envelope for payment, order, charge, session and refund projections."""

    connector = models.ForeignKey(Installation, on_delete=models.PROTECT)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True)
    kind = models.CharField(max_length=24)
    source_id = models.CharField(max_length=255)
    mode = models.CharField(max_length=4)
    status = models.CharField(max_length=32)
    amount_minor = models.BigIntegerField()
    received_minor = models.BigIntegerField(default=0)
    refunded_minor = models.BigIntegerField(default=0)
    capturable_minor = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3)
    exponent = models.PositiveSmallIntegerField()
    parent_id = models.CharField(max_length=255, blank=True)
    transaction_id = models.CharField(max_length=255, blank=True)
    pi_id = models.CharField(max_length=255, blank=True, db_index=True)
    charge_id = models.CharField(max_length=255, blank=True)
    session_id = models.CharField(max_length=255, blank=True)
    method = models.CharField(max_length=64, blank=True)
    capture_method = models.CharField(max_length=32, blank=True)
    store_hint = models.CharField(max_length=64, blank=True)
    order_hint = models.CharField(max_length=20, blank=True, db_index=True)
    created_at_source = models.DateTimeField()
    revision = models.DateTimeField()
    paid_at = models.DateTimeField(null=True)
    succeeded_at = models.DateTimeField(null=True)
    observation = models.ForeignKey(Observation, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connector", "kind", "source_id"], name="projection_namespace_uniq"),
            models.CheckConstraint(
                condition=models.Q(
                    amount_minor__gte=0, received_minor__gte=0, refunded_minor__gte=0, capturable_minor__gte=0
                ),
                name="projection_nonnegative_amounts",
            ),
            models.CheckConstraint(condition=models.Q(exponent__lte=3), name="projection_exponent_range"),
            models.CheckConstraint(
                condition=models.Q(currency__regex=r"^[A-Z]{3}$"), name="projection_currency_format"
            ),
        ]
        indexes = [
            models.Index(fields=["organization_id", "store", "mode", "kind", "revision"]),
            models.Index(fields=["connector", "kind", "parent_id"]),
        ]


class Cursor(TenantModel):
    connector = models.ForeignKey(Installation, on_delete=models.CASCADE)
    object_class = models.CharField(max_length=32)
    covered_from = models.DateTimeField(null=True)
    covered_through = models.DateTimeField(null=True)
    observed_at = models.DateTimeField(null=True)
    complete = models.BooleanField(default=False)
    scan_id = models.UUIDField(null=True)
    scan_from = models.DateTimeField(null=True)
    scan_through = models.DateTimeField(null=True)
    next_page = models.PositiveIntegerField(default=1)
    scan_failed = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connector", "object_class"], name="cursor_class_uniq")]


class IdempotencyRecord(TenantModel):
    principal = models.CharField(max_length=64)
    route = models.CharField(max_length=200)
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    response = models.JSONField()
    response_status = models.PositiveSmallIntegerField()
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "principal", "route", "key"], name="idempotency_scoped_key_uniq"
            )
        ]


class Quota(TenantModel):
    bucket = models.CharField(max_length=128)
    window = models.BigIntegerField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization_id", "bucket", "window"], name="quota_bucket_window_uniq")
        ]


class Scan(TenantModel):
    connector = models.ForeignKey(Installation, on_delete=models.CASCADE)
    window_from = models.DateTimeField()
    window_through = models.DateTimeField()
    phase = models.PositiveSmallIntegerField(default=0)
    provider_cursor = models.CharField(max_length=255, blank=True)
    page_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, default="pending")
    repair = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["connector"], condition=models.Q(status="pending"), name="scan_one_pending_connector"
            )
        ]


class BatchPage(TenantModel):
    connector = models.ForeignKey(Installation, on_delete=models.CASCADE)
    scan_id = models.UUIDField()
    page = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    result = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connector", "scan_id", "page"], name="batch_scan_page_uniq")]
