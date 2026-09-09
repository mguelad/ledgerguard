from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand

from modules.accounts.tenancy import require_database_role
from workers.runtime import dispatch, local_once, serve


class Command(BaseCommand):
    help = "Process the durable outbox locally or through SQS"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--dispatch", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        require_database_role("ledgerguard_app")
        if options["dispatch"]:
            self.stdout.write(str(dispatch()))
        elif options["once"]:
            self.stdout.write(str(local_once()))
        else:
            serve()
