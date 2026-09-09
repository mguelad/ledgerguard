import signal
import threading
from typing import Any

from django.core.management.base import BaseCommand

from modules.accounts.tenancy import require_database_role
from workers.runtime import schedule


class Command(BaseCommand):
    help = "Enqueue one bounded task per active store and connector"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        require_database_role("ledgerguard_app")
        stop = threading.Event()
        if options["loop"]:
            signal.signal(signal.SIGTERM, lambda *args: stop.set())
            signal.signal(signal.SIGINT, lambda *args: stop.set())
        while not stop.is_set():
            schedule()
            if not options["loop"] or stop.wait(300):
                break
