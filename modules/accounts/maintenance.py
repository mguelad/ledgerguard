"""Retention and tenant erasure. Run only with the restricted maintenance database role."""

import hashlib
import json
import shutil
from datetime import timedelta
from typing import Any
from uuid import UUID

import boto3
from django.apps import apps
from django.conf import settings
from django.db import connection
from django.utils import timezone
from psycopg import sql

from modules.accounts.erasure_receipts import ErasureReceipt
from modules.accounts.models import AuditLog, Invitation, Organization, TenantDirectory
from modules.accounts.tenancy import require_database_role, tenant_scope
from modules.connectors.models import OAuthState, PairingCode, PairingLocator, ResourceLocator
from modules.findings.models import EmailDelivery, Finding, FindingEvent, Outbox, ReconciliationRun
from modules.ingestion.models import BatchPage, IdempotencyRecord, Observation, Projection, Quota, Receipt
from modules.reporting.models import Report


def require_maintenance_role() -> None:
    require_database_role("ledgerguard_maintenance")


def erase_reports(org: UUID) -> None:
    prefix = f"reports/{org}/"
    if settings.REPORT_BUCKET:
        s3 = boto3.client("s3", region_name=settings.AWS_REGION)
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=settings.REPORT_BUCKET, Prefix=prefix):
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            if objects:
                result = s3.delete_objects(Bucket=settings.REPORT_BUCKET, Delete={"Objects": objects, "Quiet": True})
                if result.get("Errors"):
                    raise RuntimeError("Report erasure incomplete")
    elif not settings.PRODUCTION:
        directory = settings.BASE_DIR / "var" / "reports" / str(org)
        if directory.is_dir():
            shutil.rmtree(directory)


def tenant_models() -> list[Any]:
    remaining = [
        m
        for label in ("accounts", "connectors", "ingestion", "findings", "reporting")
        for m in apps.get_app_config(label).get_models()
        if any(f.name == "organization_id" for f in m._meta.fields)
        and m.__name__ not in {"ResourceLocator", "PairingLocator"}
    ]
    ordered = []
    while remaining:
        referenced = {
            f.remote_field.model
            for m in remaining
            for f in m._meta.fields
            if f.remote_field is not None and (f.many_to_one or f.one_to_one) and f.remote_field.model in remaining
        }
        leaves = [m for m in remaining if m not in referenced]
        if not leaves:
            raise RuntimeError("Tenant deletion dependency cycle")
        ordered.extend(leaves)
        remaining = [m for m in remaining if m not in leaves]
    return ordered


def erase_tenant(org: UUID, *, replay_receipt: ErasureReceipt | None = None) -> str:
    require_maintenance_role()
    if replay_receipt is not None and replay_receipt.organization_id != org:
        raise ValueError("Erasure receipt identity mismatch")
    with tenant_scope(org):
        organization = Organization.objects.select_for_update().get(id=org)
        if organization.active or not organization.deleted_at:
            raise ValueError("Tenant deletion has not been requested")
        erase_reports(org)
        counts = {}
        with connection.cursor() as cursor:
            for model in tenant_models():
                table = sql.Identifier(model._meta.db_table)
                cursor.execute(sql.SQL("DELETE FROM {} WHERE organization_id=%s").format(table), [str(org)])
                counts[model._meta.db_table] = cursor.rowcount
            for model in tenant_models():
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE organization_id=%s").format(
                        sql.Identifier(model._meta.db_table)
                    ),
                    [str(org)],
                )
                if cursor.fetchone()[0]:
                    raise RuntimeError("Tenant erasure verification failed")
        ResourceLocator.objects.filter(organization_id=org).delete()
        PairingLocator.objects.filter(organization_id=org).delete()
        receipt = hashlib.sha256(
            json.dumps({"organization_id": str(org), "counts": counts}, sort_keys=True).encode()
        ).hexdigest()
        # A restore must preserve the original durable receipt, not rewrite history
        # or require write access to the external ledger it is replaying.
        if replay_receipt is not None:
            receipt = replay_receipt.digest
        TenantDirectory.objects.filter(id=org).update(
            active=False,
            erased_at=replay_receipt.erased_at if replay_receipt else timezone.now(),
            erasure_digest=receipt,
        )
        if replay_receipt is not None:
            return receipt
        if settings.DELETION_LEDGER_BUCKET:
            boto3.client("s3", region_name=settings.AWS_REGION).put_object(
                Bucket=settings.DELETION_LEDGER_BUCKET,
                Key=f"erasures/{org}.json",
                Body=json.dumps(
                    {"organization_id": str(org), "erased_at": timezone.now().isoformat(), "digest": receipt}
                ).encode(),
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=settings.KMS_KEY_ID,
            )
        elif settings.PRODUCTION:
            raise RuntimeError("A durable erasure ledger is required")
        # The verification receipt contains counts and an opaque tenant ID, never source records.
        return receipt


def retain(org: UUID) -> None:
    require_maintenance_role()
    with tenant_scope(org):
        organization = Organization.objects.get(id=org)
        now = timezone.now()
        cutoff = now - timedelta(days=organization.retention_days)
        IdempotencyRecord.objects.filter(expires_at__lt=now).delete()
        Receipt.objects.filter(created_at__lt=now - timedelta(days=2), status__in=["normalized", "ignored"]).delete()
        BatchPage.objects.filter(created_at__lt=now - timedelta(days=2)).delete()
        Quota.objects.filter(created_at__lt=now - timedelta(days=2)).delete()
        ephemeral_models: list[tuple[Any, str]] = [(PairingCode, "code_hash"), (Invitation, "token_hash")]
        for model, hash_field in ephemeral_models:
            expired_codes = model.objects.filter(expires_at__lt=now - timedelta(days=1))
            PairingLocator.objects.filter(code_hash__in=expired_codes.values_list(hash_field, flat=True)).delete()
            expired_codes.delete()
        OAuthState.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()
        EmailDelivery.objects.filter(event__isnull=True, created_at__lt=cutoff).delete()
        # Retain unresolved cases and observations referenced by current projections.
        expired = Finding.objects.filter(state="resolved", last_seen__lt=cutoff)
        EmailDelivery.objects.filter(event__finding__in=expired).delete()
        FindingEvent.objects.filter(finding__in=expired).delete()
        expired.delete()
        ReconciliationRun.objects.filter(created_at__lt=cutoff, findingevent__isnull=True).delete()
        retained_ids: set[str] = set()
        for identities in Finding.objects.exclude(state="resolved").values_list("identities", flat=True):
            retained_ids.update(i.split(":", 1)[1] for i in identities if ":" in i)
        Projection.objects.filter(revision__lt=cutoff).exclude(source_id__in=retained_ids).delete()
        Observation.objects.filter(created_at__lt=cutoff, projection__isnull=True).delete()
        Outbox.objects.filter(status="done", created_at__lt=now - timedelta(days=2)).delete()
        AuditLog.objects.filter(created_at__lt=now - timedelta(days=400)).delete()
        for report in Report.objects.filter(expires_at__lt=now):
            if not settings.REPORT_BUCKET and report.object_key:
                path = settings.BASE_DIR / "var" / report.object_key
                path.unlink(missing_ok=True)
            report.delete()
