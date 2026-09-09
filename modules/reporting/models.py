from django.db import models

from modules.base import TenantModel
from modules.connectors.models import Store


class Report(TenantModel):
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True)
    requested_by = models.PositiveBigIntegerField()
    format = models.CharField(max_length=8)
    status = models.CharField(max_length=16, default="pending")
    object_key = models.CharField(max_length=255, blank=True)
    content_sha256 = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField()
