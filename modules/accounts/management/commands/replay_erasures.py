from typing import Any

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from modules.accounts.erasure_receipts import parse_receipt
from modules.accounts.maintenance import erase_tenant, require_maintenance_role
from modules.accounts.models import Organization
from modules.accounts.tenancy import tenant_scope
from modules.connectors.models import Installation, PublicKey


class Command(BaseCommand):
    help = "Reapply external erasure receipts to an isolated restored database"

    def add_arguments(self, parser: Any) -> None:
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--confirm-isolated-restore", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        parser.add_argument("--expected-database-host", required=True)
        parser.add_argument("--expected-database-name", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        require_maintenance_role()
        if (
            options["expected_database_host"] != connection.settings_dict["HOST"]
            or options["expected_database_name"] != connection.settings_dict["NAME"]
        ):
            raise CommandError("Connected database does not match the explicitly selected restore target")
        if not options["dry_run"] and not options["confirm_isolated_restore"]:
            raise CommandError("Isolated restore confirmation required")
        if not settings.DELETION_LEDGER_BUCKET:
            raise CommandError("DELETION_LEDGER_BUCKET is required")
        s3 = boto3.client("s3", region_name=settings.AWS_REGION)
        count = 0
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=settings.DELETION_LEDGER_BUCKET, Prefix="erasures/"
        ):
            for item in page.get("Contents", []):
                body = s3.get_object(Bucket=settings.DELETION_LEDGER_BUCKET, Key=item["Key"])["Body"]
                try:
                    receipt = parse_receipt(item["Key"], body.read(4097))
                except ValueError:
                    raise CommandError("Invalid external erasure receipt; keep the restore isolated") from None
                finally:
                    body.close()
                org = receipt.organization_id
                with tenant_scope(org):
                    exists = Organization.objects.filter(id=org).exists()
                    if exists and not options["dry_run"]:
                        Organization.objects.filter(id=org).update(
                            active=False, alerts_enabled=False, deleted_at=timezone.now()
                        )
                        Installation.objects.update(
                            status="disconnected", credential_ciphertext={}, webhook_ciphertext={}
                        )
                        PublicKey.objects.update(revoked_at=timezone.now())
                if exists:
                    if not options["dry_run"]:
                        erase_tenant(org, replay_receipt=receipt)
                    count += 1
        action = "would be reapplied" if options["dry_run"] else "reapplied"
        self.stdout.write(f"Erasure receipts {action}: {count}")
