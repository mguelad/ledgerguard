import hashlib
from typing import Any
from uuid import UUID

import boto3
from django.conf import settings
from django.utils import timezone

from apps.control_plane.errors import Problem
from modules.findings.models import Finding
from modules.reporting.models import Report

from .exports import PLAYBOOKS, csv_report, html_report, pdf_report


def finding_row(finding: Finding) -> dict[str, Any]:
    return {
        "id": str(finding.id),
        "finding_key": finding.finding_key,
        "rule": finding.rule_code,
        "severity": finding.severity,
        "state": finding.state,
        "store": finding.store.name,
        "mode": finding.mode,
        "amount_minor": finding.risk_amount_minor,
        "currency": finding.currency,
        "exponent": finding.exponent,
        "confidence": finding.match_confidence,
        "first_seen": finding.first_seen.isoformat(),
        "last_seen": finding.last_seen.isoformat(),
        "identities": "; ".join(finding.identities),
        "next_step": PLAYBOOKS[finding.rule_code][1],
    }


def generate(report_id: UUID) -> None:
    report = Report.objects.select_for_update().get(id=report_id)
    if report.status == "complete":
        return
    query = Finding.objects.select_related("store").order_by("first_seen")
    if report.store_id:
        query = query.filter(store_id=report.store_id)
    if query.count() > 10000:
        raise Problem("REPORT_LIMIT_EXCEEDED", 422)
    rows = [finding_row(f) for f in query]
    content = {"csv": csv_report, "html": html_report, "pdf": pdf_report}[report.format](rows)
    key = f"reports/{report.organization_id}/{report.id}.{report.format}"
    if settings.REPORT_BUCKET:
        boto3.client("s3", region_name=settings.AWS_REGION).put_object(
            Bucket=settings.REPORT_BUCKET,
            Key=key,
            Body=content,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=settings.KMS_KEY_ID,
            ContentType={"csv": "text/csv", "html": "text/html", "pdf": "application/pdf"}[report.format],
        )
    else:
        if settings.PRODUCTION:
            raise Problem("REPORT_STORAGE_UNAVAILABLE", 503)
        path = settings.BASE_DIR / "var" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    report.object_key = key
    report.status = "complete"
    report.content_sha256 = hashlib.sha256(content).hexdigest()
    report.save()


def download(report: Report) -> tuple[bytes, str] | str:
    if report.status != "complete" or report.expires_at <= timezone.now():
        raise Problem("REPORT_UNAVAILABLE", 404)
    if settings.REPORT_BUCKET:
        return str(
            boto3.client("s3", region_name=settings.AWS_REGION).generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.REPORT_BUCKET,
                    "Key": report.object_key,
                    "ResponseContentDisposition": f'attachment; filename="ledgerguard-{report.id}.{report.format}"',
                },
                ExpiresIn=60,
            )
        )
    return (settings.BASE_DIR / "var" / report.object_key).read_bytes(), report.format
