"""Validate the minimal external record before using it to erase restored data."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ErasureReceipt:
    organization_id: UUID
    erased_at: datetime
    digest: str


def parse_receipt(key: str, raw: bytes) -> ErasureReceipt:
    try:
        if len(raw) > 4096:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"organization_id", "erased_at", "digest"}:
            raise ValueError
        org = UUID(value["organization_id"])
        timestamp = datetime.fromisoformat(value["erased_at"])
        if (
            key != f"erasures/{org}.json"
            or timestamp.utcoffset() is None
            or not isinstance(value["digest"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["digest"])
        ):
            raise ValueError
        return ErasureReceipt(org, timestamp, value["digest"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Invalid external erasure receipt") from None
