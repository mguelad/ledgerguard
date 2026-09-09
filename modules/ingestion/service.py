from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.control_plane.errors import Problem
from modules.connectors.models import Installation, StoreStripeLink
from modules.findings.models import Outbox
from modules.ingestion.models import BatchPage, Cursor, Observation, Projection, Quota
from modules.ingestion.validation import digest, timestamp, utc
from packages.reconciliation_core.engine import canonical
from packages.reconciliation_core.money import CURRENCY_TABLE_VERSION, parse_woo


def quota(org: UUID, bucket: str, limit: int, seconds: int = 60) -> None:
    window = int(timezone.now().timestamp()) // seconds
    record, _ = Quota.objects.get_or_create(organization_id=org, bucket=bucket, window=window)
    record = Quota.objects.select_for_update().get(pk=record.pk)
    if record.count >= limit:
        raise Problem("RATE_LIMITED", 429)
    record.count += 1
    record.save(update_fields=["count"])


def enqueue(org: UUID, task: str, resource: UUID, key: str) -> Outbox:
    record, _ = Outbox.objects.get_or_create(
        dedupe_key=key, defaults={"organization_id": org, "task_type": task, "resource_id": resource}
    )
    return record


def reconcile_later(connector: Installation, reason: str) -> None:
    stores = (
        [connector.store_id]
        if connector.store_id
        else list(StoreStripeLink.objects.filter(stripe=connector).values_list("store_id", flat=True))
    )
    for store_id in stores:
        if store_id:
            enqueue(connector.organization_id, "reconcile_store", store_id, f"reconcile:{store_id}:{reason}")


def upsert(
    connector: Installation, kind: str, source_id: str, revision: datetime, data: dict[str, Any]
) -> tuple[Projection, str]:
    """Caller holds the connector lock. Observation and projection commit together."""
    content_hash = digest(canonical(data).encode())
    current = Projection.objects.select_for_update().filter(connector=connector, kind=kind, source_id=source_id).first()
    if current and current.observation.content_hash == content_hash:
        return current, "duplicate"
    observation, _ = Observation.objects.get_or_create(
        connector=connector,
        kind=kind,
        source_id=source_id,
        revision=revision,
        content_hash=content_hash,
        defaults={
            "organization_id": connector.organization_id,
            "data": data,
            "currency_table_version": CURRENCY_TABLE_VERSION,
        },
    )
    if current and revision < current.revision:
        return current, "duplicate"
    if current and revision == current.revision:
        connector.last_error_code = "SOURCE_REVISION_CONFLICT"
        connector.save(update_fields=["last_error_code"])
        Cursor.objects.filter(connector=connector).update(complete=False)
        raise Problem("SOURCE_REVISION_CONFLICT", 409)
    fields = {key: value for key, value in data.items() if key in {f.name for f in Projection._meta.fields}}
    for key in ["created_at_source", "paid_at", "succeeded_at"]:
        if fields.get(key):
            fields[key] = timestamp(fields[key])
    if kind == "payment" and data["status"] == "succeeded":
        fields["succeeded_at"] = current.succeeded_at if current and current.succeeded_at else revision
    fields.update(
        organization_id=connector.organization_id,
        store_id=connector.store_id,
        mode=connector.mode,
        revision=revision,
        observation=observation,
        updated_at=timezone.now(),
    )
    if current:
        for key, value in fields.items():
            setattr(current, key, value)
        current.lock_version += 1
        current.save()
    else:
        current = Projection.objects.create(connector=connector, kind=kind, source_id=source_id, **fields)
    return current, "accepted"


def normalize_woo(record: dict[str, Any]) -> dict[str, Any]:
    money = parse_woo(record["amount"], record["currency"])
    data: dict[str, Any] = {
        "status": record["status"],
        "amount_minor": money.minor,
        "currency": money.currency,
        "exponent": money.exponent,
        "created_at_source": record["created_at"],
    }
    if record["kind"] == "order":
        refunded = parse_woo(record["total_refunded"], record["currency"])
        data.update(
            refunded_minor=refunded.minor,
            method=record["payment_method"],
            transaction_id=record["transaction_id"],
            pi_id=record["stripe_ids"].get("payment_intent", ""),
            charge_id=record["stripe_ids"].get("charge", ""),
            session_id=record["stripe_ids"].get("checkout_session", ""),
            paid_at=record["paid_at"],
            refund_ids=record["refund_ids"],
        )
    else:
        data["parent_id"] = record["parent_id"]
    return data


def accept_woo_batch(connector: Installation, envelope: dict[str, Any]) -> dict[str, Any]:
    page_hash = digest(canonical({k: v for k, v in envelope.items() if k not in {"request_id", "sent_at"}}).encode())
    prior = BatchPage.objects.filter(connector=connector, scan_id=envelope["scan_id"], page=envelope["page"]).first()
    if prior:
        if prior.content_hash != page_hash:
            raise Problem("SCAN_PAGE_CONTENT_CONFLICT", 409)
        return {**prior.result, "request_id": envelope["request_id"], "server_time": utc(timezone.now())}
    start, end = timestamp(envelope["covered_from"]), timestamp(envelope["covered_through"])
    now = timezone.now()
    if not now - timedelta(days=36) <= start <= end <= now + timedelta(seconds=120):
        raise Problem("INVALID_COVERAGE_WINDOW", 422)
    cursor, _ = Cursor.objects.get_or_create(
        connector=connector, object_class="woo_orders", defaults={"organization_id": connector.organization_id}
    )
    cursor = Cursor.objects.select_for_update().get(pk=cursor.pk)
    scan = UUID(envelope["scan_id"])
    if cursor.scan_id != scan:
        if envelope["page"] != 1 or (
            cursor.scan_id
            and not cursor.scan_failed
            and cursor.scan_through
            and cursor.updated_at > now - timedelta(minutes=30)
        ):
            raise Problem("SCAN_SEQUENCE_CONFLICT", 409)
        cursor.scan_id, cursor.scan_from, cursor.scan_through, cursor.next_page, cursor.scan_failed = (
            scan,
            start,
            end,
            1,
            False,
        )
        cursor.complete = False
        Cursor.objects.filter(connector=connector, object_class="woo_refunds").update(complete=False)
    if cursor.next_page != envelope["page"] or cursor.scan_from != start or cursor.scan_through != end:
        raise Problem("SCAN_SEQUENCE_CONFLICT", 409)
    results = []
    coverage_advanced = False
    for record in envelope["records"]:
        try:
            with transaction.atomic():
                revision = timestamp(record["revision"])
                if revision > now + timedelta(seconds=120):
                    raise ValueError("Future revision")
                _, disposition = upsert(
                    connector,
                    "order" if record["kind"] == "order" else "woo_refund",
                    record["source_id"],
                    revision,
                    normalize_woo(record),
                )
                results.append({"source_id": record["source_id"], "kind": record["kind"], "status": disposition})
        except (ValueError, Problem) as exc:
            cursor.scan_failed = True
            if isinstance(exc, Problem) and exc.code == "SOURCE_REVISION_CONFLICT":
                connector.last_error_code = exc.code
                connector.save(update_fields=["last_error_code"])
            results.append(
                {
                    "source_id": record["source_id"],
                    "kind": record["kind"],
                    "status": "rejected",
                    "code": "INVALID_SOURCE_FACT",
                }
            )
    cursor.next_page += 1
    cursor.updated_at = now
    if envelope["final_page"]:
        contiguous = cursor.covered_through is None or start <= cursor.covered_through
        if not cursor.scan_failed and contiguous:
            cursor.covered_from = min(cursor.covered_from or start, start)
            cursor.covered_through = max(cursor.covered_through or end, end)
            cursor.observed_at, cursor.complete = now, True
            coverage_advanced = True
            Cursor.objects.update_or_create(
                connector=connector,
                object_class="woo_refunds",
                defaults={
                    "organization_id": connector.organization_id,
                    "covered_from": cursor.covered_from,
                    "covered_through": cursor.covered_through,
                    "observed_at": now,
                    "complete": True,
                },
            )
        elif cursor.scan_failed:
            cursor.complete = False
            Cursor.objects.filter(connector=connector, object_class="woo_refunds").update(complete=False)
        cursor.scan_id = None
        cursor.scan_through = None
    cursor.save()
    reconcile_later(connector, str(envelope["request_id"]))
    result = {
        "request_id": envelope["request_id"],
        "results": results,
        "coverage_advanced": coverage_advanced,
        "server_time": utc(now),
    }
    BatchPage.objects.create(
        organization_id=connector.organization_id,
        connector=connector,
        scan_id=envelope["scan_id"],
        page=envelope["page"],
        content_hash=page_hash,
        result=result,
    )
    return result


def advance_stripe_coverage(connector: Installation, start: datetime, end: datetime) -> None:
    for kind in ["stripe_payments", "stripe_refunds"]:
        cursor, _ = Cursor.objects.get_or_create(
            connector=connector, object_class=kind, defaults={"organization_id": connector.organization_id}
        )
        if cursor.covered_through is not None and start > cursor.covered_through:
            raise Problem("COVERAGE_GAP", 409)
        cursor.covered_from = min(cursor.covered_from or start, start)
        cursor.covered_through = max(cursor.covered_through or end, end)
        cursor.observed_at = timezone.now()
        cursor.complete = True
        cursor.lock_version += 1
        cursor.save()
