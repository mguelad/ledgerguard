import secrets
import time
import uuid
from typing import Any

from django.db import models
from django.utils import timezone


def uuid7() -> uuid.UUID:
    return uuid.UUID(
        int=((time.time_ns() // 1_000_000) << 80)
        | (7 << 76)
        | (secrets.randbits(12) << 64)
        | (2 << 62)
        | secrets.randbits(62)
    )


class TenantModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    organization_id = models.UUIDField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now)
    lock_version = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True


class ImmutableTenantModel(TenantModel):
    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValueError("Append-only record")
        return super().save(*args, **kwargs)
