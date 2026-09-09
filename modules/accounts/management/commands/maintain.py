import signal
import threading
from typing import Any

from django.core.management.base import BaseCommand

from modules.accounts.maintenance import erase_tenant, require_maintenance_role, retain
from modules.accounts.models import Organization, TenantDirectory
from modules.accounts.tenancy import tenant_scope


class Command(BaseCommand):
    help = "Execute requested tenant deletion and bounded retention as the maintenance role"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        require_maintenance_role()
        stop = threading.Event()
        if options["loop"]:
            signal.signal(signal.SIGTERM, lambda *args: stop.set())
            signal.signal(signal.SIGINT, lambda *args: stop.set())
        while not stop.is_set():
            self.run_once()
            if not options["loop"] or stop.wait(3600):
                break

    def run_once(self) -> None:
        for org in TenantDirectory.objects.filter(active=True).values_list("id", flat=True).iterator():
            with tenant_scope(org):
                pending = Organization.objects.filter(id=org, active=False, deleted_at__isnull=False).exists()
            if pending:
                self.stdout.write(f"Deletion receipt: {erase_tenant(org)}")
            else:
                retain(org)
