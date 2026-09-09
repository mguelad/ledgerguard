# LedgerGuard

[![Source checks](https://github.com/mguelad/ledgerguard/actions/workflows/checks.yml/badge.svg)](https://github.com/mguelad/ledgerguard/actions/workflows/checks.yml)
[![WooCommerce compatibility](https://github.com/mguelad/ledgerguard/actions/workflows/plugin.yml/badge.svg)](https://github.com/mguelad/ledgerguard/actions/workflows/plugin.yml)

Read-only payment integrity monitoring for WooCommerce agencies. LedgerGuard compares Stripe payment outcomes with WooCommerce order and refund records. It produces reproducible findings, evidence and an investigation history. It never captures payments, issues refunds, changes orders or controls fulfilment.

The v1 implementation follows the engineer handoff: Django 5.2 LTS, a framework-free reconciliation engine, PostgreSQL 17 with FORCE row-level security, Stripe App OAuth, an outbound signed WooCommerce plugin, transactional work queues and an AWS EU deployment foundation.

Version 0.1.0 is a source handoff for pilot verification. Repository CI passes 192 tests on native PostgreSQL with 84.35% statement coverage, the desktop/mobile browser journeys, and all 16 WordPress/WooCommerce combinations. Provider and production-environment gates remain open. See the [verification record](docs/verification.md) before launching.

## Run locally

Install Docker Engine with Compose v2, then run:

```sh
python3 scripts/configure_local.py
docker compose up --build -d
docker compose exec api python manage.py seed_demo
```

Choose a password when prompted. Open http://localhost:8000 and sign in as `demo`. The sample tenant uses synthetic records and never contacts Stripe. Its coverage becomes stale over time, just as an interrupted real connector does. Run `seed_demo --username demo2` for a fresh example.

For an empty application, use `docker compose exec api python manage.py createsuperuser` and create an organization after signing in. Django staff status does not grant tenant access. Production and staging require Cognito with MFA; local passwords are disabled there.

The stack includes PostgreSQL, a migration task, the API, a worker, scheduler and a separate maintenance process. Database access is private to Compose. `docker compose down` retains the database volume. Do not remove volumes containing data you need.

## Development

Python 3.13 is the supported interpreter. Dependency versions and package hashes are locked.

```sh
uv sync --extra dev --frozen
make check
uv run --extra dev pytest
```

Database tests require an **isolated disposable** PostgreSQL database with a name ending in `_test`. CI bootstraps restricted roles using an administrator connection; application assertions run as `ledgerguard_app`, never as a superuser or table owner.

```sh
DATABASE_URL=postgresql://postgres:local-test-password@localhost:5432/ledgerguard_test \
DB_SSLMODE=disable LEDGERGUARD_INTEGRATION=1 uv run --extra dev pytest \
  --cov=apps --cov=modules --cov=packages --cov=workers --cov-fail-under=80
```

Never use a valuable database for this command. See [verification](docs/verification.md) for measured results and limitations.

## Connect a store

Follow [connector setup](docs/connectors.md). Register a read-only Stripe App, configure separate environment credentials, pair the plugin, prove scan completeness, verify an exact payment/order identity and review a dry run before enabling alerts.

```sh
uv run python scripts/build_plugin.py --origin https://your-verified-domain.example
```

Replace that origin with your deployment's actual HTTPS origin. The build emits an installable ZIP, checksum and file manifest in `dist/`. The source's `.invalid` destination is deliberately unusable.

## Offline audit

```sh
uv run ledgerguard-audit --stripe-csv fixtures/csv/stripe.csv \
  --woo-csv fixtures/csv/woo.csv --refund-csv fixtures/csv/refunds.csv \
  --organization northwind --store store_one --account acct_one --mode test \
  --as-of 2026-09-05T12:00:00Z --covered-from 2026-08-06T12:00:00Z \
  --covered-through 2026-09-05T11:59:00Z --output var/audit
```

Outputs include CSV, HTML, PDF and structured evidence. The command reads normalized source files in place without retaining an import copy. See [CSV contracts](docs/csv-audit.md). The operator must establish export completeness; a file cannot prove that omitted rows do not exist.

## Code map

| Path | Responsibility |
| --- | --- |
| `packages/reconciliation_core` | Money, immutable domain inputs, matching and rules PI-001–PI-010 |
| `apps/control_plane` | HTTP APIs, authentication boundaries and server-rendered dashboard |
| `modules/accounts` | Membership, RLS scope, audit, support and retention |
| `modules/connectors` | Stripe OAuth, read access, encryption and signed Woo requests |
| `modules/ingestion` | Immutable observations, projections, receipts and complete scans |
| `modules/findings` | Reconciliation runs, lifecycle, evidence and notification delivery |
| `modules/reporting` | Private report generation and offline audit |
| `workers` | Transactional outbox dispatch, retries and queue consumers |
| `plugins/ledgerguard-woocommerce` | Woo CRUD extraction and Action Scheduler jobs |
| `contracts`, `fixtures`, `tests` | Executable interfaces and synthetic verification |
| `infra`, `docs` | Deployment definitions, decisions and operations |

## Handoff

Read [architecture](docs/architecture.md), [traceability](docs/traceability.md), [deployment](docs/deployment.md), [security](docs/security-model.md) and [release gates](docs/release-gates.md).

The canonical repository is [mguelad/ledgerguard](https://github.com/mguelad/ledgerguard). Start with the [documentation index](docs/README.md), follow the [contribution guide](CONTRIBUTING.md) for engineering changes, and report vulnerabilities through the [security policy](SECURITY.md). Keep secrets, Terraform state, merchant exports and reports out of version control. Uploading the source does not deploy infrastructure. Configure protected environments before releasing.

The application is proprietary by default; the Woo plugin is GPL-2.0-or-later. [Third-party dependencies](THIRD_PARTY_NOTICES.md) retain their licenses. The source implementation and a verified live deployment are separate deliverables: provider authorization, native database concurrency, AWS recovery, sustained load, accessibility review and independent penetration testing require target-environment evidence.
