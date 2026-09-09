"""Create an isolated synthetic store for local inspection."""

import getpass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from modules.accounts.models import Membership, Organization, TenantDirectory
from modules.accounts.tenancy import tenant_scope
from modules.base import uuid7
from modules.connectors.models import Installation, ResourceLocator, Store, StoreStripeLink
from modules.findings.service import reconcile
from modules.ingestion.models import Cursor
from modules.ingestion.service import upsert
from modules.ingestion.validation import utc


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--username", default="demo")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.PRODUCTION:
            raise CommandError("Synthetic seeding is only available locally")
        username = options["username"]
        if get_user_model().objects.filter(username=username).exists():
            raise CommandError("Choose a new username to create a separate example")
        password = getpass.getpass("Local demo password (at least 12 characters): ")
        if len(password) < 12:
            raise CommandError("A password of at least 12 characters is required")
        now = timezone.now()
        org = uuid7()
        with tenant_scope(org):
            user = get_user_model().objects.create_user(
                username=username, email=f"{username}@example.com", password=password
            )
            TenantDirectory.objects.create(id=org)
            Organization.objects.create(id=org, organization_id=org, name="Northwind Commerce")
            Membership.objects.create(organization_id=org, user=user, role="owner")
            store = Store.objects.create(
                organization_id=org, name="Northwind Supply", hostname="shop.northwind.example", mode="test"
            )
            ResourceLocator.objects.create(id=store.id, organization_id=org, kind="store")
            woo = Installation.objects.create(
                organization_id=org, store=store, kind="woo", mode="test", status="active", health={"synthetic": True}
            )
            stripe = Installation.objects.create(
                organization_id=org,
                kind="stripe",
                account_id="acct_demo",
                mode="test",
                status="active",
                health={"synthetic": True},
            )
            for row in [woo, stripe]:
                ResourceLocator.objects.create(id=row.id, organization_id=org, kind="connector")
            StoreStripeLink.objects.create(
                organization_id=org,
                store=store,
                stripe=stripe,
                status="verified",
                owner_confirmed_at=now,
                merchant_confirmed_at=now,
                verified_payment_id="pi_demo1",
            )
            for source, connector in [
                ("woo_orders", woo),
                ("woo_refunds", woo),
                ("stripe_payments", stripe),
                ("stripe_refunds", stripe),
            ]:
                Cursor.objects.create(
                    organization_id=org,
                    connector=connector,
                    object_class=source,
                    covered_from=now - timedelta(days=35),
                    covered_through=now - timedelta(minutes=1),
                    observed_at=now,
                    complete=True,
                )
            date = utc(now - timedelta(hours=3))
            for index, amount, status in [(1, 12995, "pending"), (2, 4500, "processing"), (3, 8900, "completed")]:
                pid = f"pi_demo{index}"
                upsert(
                    woo,
                    "order",
                    str(index),
                    now - timedelta(hours=2),
                    {
                        "status": status,
                        "amount_minor": amount,
                        "currency": "EUR",
                        "exponent": 2,
                        "created_at_source": date,
                        "refunded_minor": 0,
                        "method": "stripe",
                        "transaction_id": pid,
                        "pi_id": pid,
                        "charge_id": "",
                        "session_id": "",
                        "paid_at": date if status != "pending" else None,
                        "refund_ids": [],
                    },
                )
                upsert(
                    stripe,
                    "payment",
                    pid,
                    now - timedelta(hours=2),
                    {
                        "status": "succeeded" if index != 2 else "processing",
                        "amount_minor": amount,
                        "received_minor": amount if index != 2 else 0,
                        "currency": "EUR",
                        "exponent": 2,
                        "created_at_source": date,
                        "method": "card",
                        "capture_method": "automatic",
                        "charge_id": "",
                        "order_hint": str(index),
                        "store_hint": str(store.id),
                    },
                )
            reconcile(store.id, now)
        self.stdout.write("Synthetic organization created. Sign in at " + settings.PUBLIC_URL + " as " + username + ".")
