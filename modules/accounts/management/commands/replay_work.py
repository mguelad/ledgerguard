from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from modules.accounts.tenancy import audit, require_database_role, tenant_scope
from modules.findings.models import Outbox


class Command(BaseCommand):
    help = "Replay one reviewed exhausted work intent without changing its tenant or resource"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--organization", type=UUID, required=True)
        parser.add_argument("--work", type=UUID, required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        require_database_role("ledgerguard_app")
        if not 10 <= len(options["reason"]) <= 500:
            raise CommandError("A review reason of 10–500 characters is required")
        with tenant_scope(options["organization"]):
            row = Outbox.objects.select_for_update().filter(id=options["work"], status="dead").first()
            if row is None:
                raise CommandError("No exhausted work exists in that organization")
            row.status = "pending"
            row.attempts = 0
            row.available_at = timezone.now()
            row.lease_until = None
            row.save()
            audit(row.organization_id, "operator", "work.replayed", row.id, {"reason": options["reason"]})
        self.stdout.write("Work scheduled for reviewed replay.")
