"""Reference-only SQS dispatch backed by transactional outbox rows."""

import json
import logging
import secrets
import signal
import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import boto3
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone

from apps.control_plane.errors import Problem
from apps.control_plane.telemetry import metric, new_trace, trace_context
from modules.accounts.models import AuditLog, Organization, TenantDirectory
from modules.accounts.tenancy import audit, tenant_scope
from modules.connectors.models import Installation, Store
from modules.connectors.stripe import ConnectorFailure
from modules.findings.models import EmailDelivery, Outbox
from modules.ingestion.models import Cursor, Receipt, Scan
from modules.ingestion.service import enqueue
from modules.ingestion.validation import validate

log = logging.getLogger(__name__)
STOP = False


def _stop(signum: int, frame: Any) -> None:
    global STOP
    STOP = True


def handle(row: Outbox) -> None:
    if row.task_type == "stripe_receipt":
        from modules.connectors.stripe import process_receipt

        process_receipt(row.resource_id)
    elif row.task_type in {"stripe_poll", "stripe_repair"}:
        from modules.connectors.stripe import poll

        poll(row.resource_id, row.task_type == "stripe_repair")
    elif row.task_type == "stripe_scan_page":
        from modules.connectors.stripe import scan_page

        scan_page(row.resource_id)
    elif row.task_type == "reconcile_store":
        from modules.findings.service import reconcile

        reconcile(row.resource_id)
    elif row.task_type == "notify":
        from modules.findings.notifications import prepare

        prepare(row.resource_id)
    elif row.task_type == "security_notify":
        from modules.findings.notifications import security

        security(row.resource_id)
    elif row.task_type == "deliver_email":
        from modules.findings.notifications import deliver

        deliver(row.resource_id)
    elif row.task_type == "notify_digest":
        from modules.findings.notifications import digest

        digest(row.organization_id, row.dedupe_key)
    elif row.task_type == "report":
        from modules.reporting.service import generate

        generate(row.resource_id)
    elif row.task_type in {"delete_tenant", "retention"}:
        raise ProblemMaintenance("A maintenance worker is required")
    else:
        raise ValueError("Unknown task type")


class InvalidMessage(Exception):
    pass


class ProblemMaintenance(Exception):
    pass


def schedule() -> None:
    now = timezone.now()
    bucket = int(now.timestamp()) // 300
    for org in TenantDirectory.objects.filter(active=True).values_list("id", flat=True).iterator():
        with tenant_scope(org):
            if not Organization.objects.filter(id=org, active=True).exists():
                continue
            for store in Store.objects.filter(active=True):
                enqueue(org, "reconcile_store", store.id, f"periodic-reconcile:{store.id}:{bucket}")
            for connector in Installation.objects.filter(kind="stripe", status="active").exclude(
                health__synthetic=True
            ):
                task = "stripe_repair" if bucket % 288 == 0 else "stripe_poll"
                enqueue(org, task, connector.id, f"periodic-stripe:{connector.id}:{bucket}")


def message(row: Outbox) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "message_id": str(row.id),
        "task_type": row.task_type,
        "organization_id": str(row.organization_id),
        "resource_id": str(row.resource_id),
        "attempt_context": row.attempts,
        "traceparent": new_trace(trace_context.get()),
    }


def queue_for(task: str) -> str:
    kind = (
        "notification"
        if task in {"notify", "security_notify", "deliver_email", "notify_digest"}
        else "reconciliation"
        if task == "reconcile_store"
        else "reporting"
        if task == "report"
        else "repair"
        if task in {"stripe_poll", "stripe_repair", "stripe_scan_page"}
        else "ingestion"
    )
    return str(settings.QUEUE_URLS.get(kind, settings.QUEUE_URL))


def dispatch() -> int:
    if not settings.QUEUE_URL and not settings.QUEUE_URLS:
        return 0
    sqs = boto3.client("sqs", region_name=settings.AWS_REGION)
    count = 0
    for org in TenantDirectory.objects.filter(active=True).values_list("id", flat=True).iterator():
        with tenant_scope(org):
            rows = (
                Outbox.objects.select_for_update(skip_locked=True)
                .filter(
                    Q(status="pending") | Q(status="queued", lease_until__lt=timezone.now()),
                    available_at__lte=timezone.now(),
                )
                .order_by("available_at")[:100]
            )
            for row in rows:
                if row.task_type in {"delete_tenant", "retention"}:
                    continue
                sqs.send_message(
                    QueueUrl=queue_for(row.task_type), MessageBody=json.dumps(message(row), separators=(",", ":"))
                )
                row.status = "queued"
                row.lease_until = timezone.now() + timedelta(minutes=20)
                row.save(update_fields=["status", "lease_until"])
                count += 1
    return count


def run_message(value: dict[str, Any]) -> bool:
    token = trace_context.set(new_trace(str(value.get("traceparent", ""))))
    started = time.monotonic()
    try:
        result = _run_message(value)
        if not result:
            metric("WorkFailed", 1)
        return result
    finally:
        metric("WorkDurationMs", (time.monotonic() - started) * 1000)
        trace_context.reset(token)


def _run_message(value: dict[str, Any]) -> bool:
    validate(value, "queue-1.json")
    org = UUID(value["organization_id"])
    row_id = UUID(value["message_id"])
    try:
        with tenant_scope(org):
            row = Outbox.objects.select_for_update().filter(id=row_id).first()
            if row is None:
                raise ValueError("Unknown work reference")
            if row.task_type != value["task_type"] or str(row.resource_id) != value["resource_id"]:
                raise InvalidMessage("Work reference mismatch")
            if row.status == "done":
                return True
            if row.status == "dead":
                return False
            if row.available_at > timezone.now():
                return False
            organization_active = Organization.objects.filter(id=org, active=True).exists()
            deletion_notice = (
                row.task_type == "security_notify"
                and AuditLog.objects.filter(id=row.resource_id, action="organization.deletion_requested").exists()
            )
            deletion_delivery = (
                row.task_type == "deliver_email"
                and EmailDelivery.objects.filter(
                    id=row.resource_id, security_event__action="organization.deletion_requested"
                ).exists()
            )
            if not organization_active and not deletion_notice and not deletion_delivery:
                row.status = "done"
                row.save(update_fields=["status"])
                return True
            handle(row)
            row.status = "done"
            row.lease_until = None
            row.last_error_code = ""
            row.save()
            return True
    except InvalidMessage:
        return False
    except Exception as exc:
        code = exc.code if isinstance(exc, ConnectorFailure) else type(exc).__name__
        with tenant_scope(org):
            row = Outbox.objects.select_for_update().filter(id=row_id).first()
            if row is None:
                raise
            row.attempts += 1
            row.last_error_code = code[:64]
            permanent = isinstance(exc, ConnectorFailure) and exc.permanent
            row.status = "dead" if permanent or row.attempts >= 8 else "pending"
            delay = min(900, 2**row.attempts) + secrets.randbelow(30)
            if isinstance(exc, ConnectorFailure):
                delay = max(delay, exc.retry_after)
            row.available_at = timezone.now() + timedelta(seconds=delay)
            row.lease_until = None
            row.save()
            if row.status == "dead" and row.task_type == "report":
                from modules.reporting.models import Report

                Report.objects.filter(id=row.resource_id).exclude(status="complete").update(status="failed")
            connector_id = None
            if row.task_type in {"stripe_poll", "stripe_repair"}:
                connector_id = row.resource_id
            elif row.task_type == "stripe_receipt":
                connector_id = Receipt.objects.filter(id=row.resource_id).values_list("connector_id", flat=True).first()
                if row.status == "dead":
                    Receipt.objects.filter(id=row.resource_id).update(status="failed")
            elif row.task_type == "stripe_scan_page":
                connector_id = Scan.objects.filter(id=row.resource_id).values_list("connector_id", flat=True).first()
                if row.status == "dead":
                    Scan.objects.filter(id=row.resource_id, status="pending").update(
                        status="failed", updated_at=timezone.now()
                    )
            if row.status == "dead" and connector_id:
                Cursor.objects.filter(connector_id=connector_id).update(complete=False)
            if permanent and connector_id:
                Installation.objects.filter(id=connector_id).update(
                    status="suspended", last_error_code=code, credential_ciphertext={}
                )
                Scan.objects.filter(connector_id=connector_id, status="pending").update(
                    status="failed", updated_at=timezone.now()
                )
                audit(org, "worker", "connector.suspended", connector_id, {"error_code": code})
            log.error(
                "worker_failed", extra={"event_code": "worker_failed", "message_id": str(row_id), "error_code": code}
            )
        return False


def local_once() -> int:
    count = 0
    for org in TenantDirectory.objects.filter(active=True).values_list("id", flat=True).iterator():
        with tenant_scope(org):
            rows = [
                message(row)
                for row in Outbox.objects.filter(status="pending", available_at__lte=timezone.now())
                .exclude(task_type__in=["retention", "delete_tenant"])
                .order_by("available_at")[:100]
            ]
        for value in rows:
            run_message(value)
            count += 1
    return count


def serve() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    queue_url = str(settings.QUEUE_URLS.get(settings.WORKER_QUEUE_CLASS, settings.QUEUE_URL))
    sqs = boto3.client("sqs", region_name=settings.AWS_REGION) if queue_url else None
    while not STOP:
        if sqs:
            dispatch()
            if settings.WORKER_QUEUE_CLASS == "notification" and settings.SES_FEEDBACK_QUEUE_URL:
                consume_feedback(sqs)
            result = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=10, VisibilityTimeout=900
            )
            for entry in result.get("Messages", []):
                try:
                    done = run_message(json.loads(entry["Body"]))
                    if done:
                        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=entry["ReceiptHandle"])
                except (ValueError, KeyError, Problem):
                    log.error("poison_message", extra={"event_code": "poison_message"})
        elif not local_once():
            time.sleep(1)


def consume_feedback(sqs: Any) -> None:
    from modules.findings.notifications import feedback

    response = sqs.receive_message(QueueUrl=settings.SES_FEEDBACK_QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=0)
    for entry in response.get("Messages", []):
        try:
            feedback(json.loads(entry["Body"]))
        except (ValueError, KeyError, TypeError, ObjectDoesNotExist):
            log.error("invalid_email_feedback", extra={"event_code": "invalid_email_feedback"})
            # Malformed feedback stays on its restricted queue for dead-letter investigation.
            continue
        sqs.delete_message(QueueUrl=settings.SES_FEEDBACK_QUEUE_URL, ReceiptHandle=entry["ReceiptHandle"])
