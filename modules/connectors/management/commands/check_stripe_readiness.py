import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from modules.accounts.models import Organization
from modules.accounts.tenancy import tenant_scope
from modules.connectors.acceptance import RESOURCES, probe, validate_samples
from modules.connectors.models import Installation
from modules.connectors.stripe import ConnectorFailure, StripeReader, refresh_access_token
from modules.ingestion.models import Cursor, Scan


class Command(BaseCommand):
    help = "Probe an installed test-mode Stripe App with ten bounded GETs and optional token rotation"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--organization", required=True, type=UUID)
        parser.add_argument("--installation", required=True, type=UUID)
        parser.add_argument("--confirm-test-account", required=True)
        parser.add_argument("--rotate-token", action="store_true", help="Exercise and persist a real OAuth refresh")
        for kind in RESOURCES:
            parser.add_argument("--" + kind.replace(".", "-").replace("_", "-") + "-id", required=True)
        parser.add_argument("--output", type=Path, default=Path("var/acceptance/stripe-reads.json"))

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.ENVIRONMENT not in {"development", "dev", "staging"}:
            raise CommandError("Run provider acceptance in development/dev/staging, never production")
        if not re.fullmatch(r"acct_[A-Za-z0-9]+", options["confirm_test_account"]):
            raise CommandError("An explicit test account ID is required")
        samples = {kind: options[kind.replace(".", "_") + "_id"] for kind in RESOURCES}
        try:
            validate_samples(samples)
        except ValueError:
            raise CommandError("Five exact synthetic test-resource IDs are required") from None
        checked: list[str] = []
        error = ""
        rotated = False
        with tenant_scope(options["organization"]):
            if not Organization.objects.filter(id=options["organization"], active=True).exists():
                raise CommandError("An active explicitly selected tenant is required")
            connector = (
                Installation.objects.select_for_update()
                .filter(
                    id=options["installation"],
                    kind="stripe",
                    mode="test",
                    status="active",
                    account_id=options["confirm_test_account"],
                )
                .first()
            )
            if connector is None:
                raise CommandError("Active test-mode authorization does not match the selected tenant/account")
            if options["rotate_token"]:
                connector.token_expires_at = timezone.now()
            version = connector.lock_version
            try:
                try:
                    with transaction.atomic():
                        refresh_access_token(connector)
                except ConnectorFailure:
                    raise
                except Exception:
                    raise ConnectorFailure("OAUTH_PERSISTENCE_UNCERTAIN", True) from None
                rotated = connector.lock_version != version
                checked = probe(StripeReader(connector), samples)
            except ConnectorFailure as exc:
                error = exc.code
                if exc.permanent:
                    connector.status, connector.last_error_code, connector.credential_ciphertext = (
                        "suspended",
                        error,
                        {},
                    )
                    connector.save(update_fields=["status", "last_error_code", "credential_ciphertext"])
                    Cursor.objects.filter(connector=connector).update(complete=False)
                    Scan.objects.filter(connector=connector, status="pending").update(
                        status="failed", updated_at=timezone.now()
                    )
            # A failed GET must still commit a successful token refresh. Raise
            # CommandError only after leaving this transaction.
        report = {
            "schema_version": 1,
            "kind": "stripe-test-resource-reads",
            "checked_at": timezone.now().isoformat(),
            "environment": settings.ENVIRONMENT,
            "api_version": settings.STRIPE_API_VERSION,
            "checked_resources": checked,
            "tokens_rotated": rotated,
            "passed": not error,
            "error_code": error,
            "production_accepted": False,
            "unverified": [
                "installed_permission_grant",
                "oauth_browser_consent",
                "webhook_rotation_recovery",
                "overlap_backfill_parity",
                "managed_sandbox_support",
            ],
        }
        output: Path = options["output"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        if error:
            raise CommandError(f"Stripe test reads failed: {error}")
        self.stdout.write("Stripe test reads passed; other provider acceptance gates remain open")
