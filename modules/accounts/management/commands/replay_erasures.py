import json
from typing import Any
from uuid import UUID

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from modules.accounts.maintenance import erase_tenant, require_maintenance_role
from modules.accounts.models import Organization
from modules.accounts.tenancy import tenant_scope
from modules.connectors.models import Installation, PublicKey


class Command(BaseCommand):
    help = "Reapply external erasure receipts to an isolated restored database"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--confirm-isolated-restore", action="store_true", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        require_maintenance_role()
        if not settings.DELETION_LEDGER_BUCKET:
            raise CommandError("DELETION_LEDGER_BUCKET is required")
        s3 = boto3.client("s3", region_name=settings.AWS_REGION)
        count = 0
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=settings.DELETION_LEDGER_BUCKET, Prefix="erasures/"
        ):
            for item in page.get("Contents", []):
                raw = s3.get_object(Bucket=settings.DELETION_LEDGER_BUCKET, Key=item["Key"])["Body"].read(4097)
                if len(raw) > 4096:
                    raise CommandError("Invalid erasure receipt size")
                receipt = json.loads(raw)
                org = UUID(receipt["organization_id"])
                if item["Key"] != f"erasures/{org}.json":
                    raise CommandError("Erasure receipt identity mismatch")
                with tenant_scope(org):
                    exists = Organization.objects.filter(id=org).exists()
                    if exists:
                        Organization.objects.filter(id=org).update(
                            active=False, alerts_enabled=False, deleted_at=timezone.now()
                        )
                        Installation.objects.update(
                            status="disconnected", credential_ciphertext={}, webhook_ciphertext={}
                        )
                        PublicKey.objects.update(revoked_at=timezone.now())
                if exists:
                    erase_tenant(org)
                    count += 1
        self.stdout.write(f"Erasure receipts reapplied: {count}")
