# Verification record

Version: **0.1.0 source handoff**. Verification date: **9 September 2026**.

The source is prepared for repository import and target-environment verification. No AWS resources were deployed and no live merchant credentials were used. This record does not constitute production acceptance.

## Repository CI follow-up — 9 September 2026

Commit `5941c8483690a9b330e0575643086a57e2c374cb` passed [Source checks](https://github.com/mguelad/ledgerguard/actions/runs/34351660793) and [WooCommerce compatibility](https://github.com/mguelad/ledgerguard/actions/runs/34351660745). Native PostgreSQL ran **192 tests with no skips and 84.35% statement coverage**, including the previously unverified authentication and concurrency cases. Docker/Compose health, desktop/mobile browser journeys, all 16 plugin combinations, Terraform provider initialization/validation, and source secret/dependency/IaC scanning also passed.

This closes the corresponding automation gaps in the historical handoff record below. The source scan has two [documented, resource-specific network exceptions](security-model.md#network-policy-and-scanner-exceptions); it does not claim that public ingress or arbitrary HTTPS destinations have no risk. AWS planning/deployment, release-image scanning, Stripe sandbox acceptance, recovery, load, accessibility review and independent security testing remain open. The machine-readable `verification-results.json` preserves the original handoff measurements.

## Executed checks

| Check | Result | Scope |
| --- | --- | --- |
| Python suite | **187 passed; 2 skipped; no warnings** | Python 3.13.14, Django 5.2.17, pytest 9.1.1; synthetic fixtures |
| Statement coverage | **84.36%: 3,079 of 3,650 statements** | `apps`, `modules`, `packages`, `workers`, including management command packages; no excluded lines; threshold 80% |
| Golden fixtures | Passed | Twenty portable cases covering all ten rule codes, aliases, ambiguity, asynchronous outcomes and compensated duplicate payments |
| Python static checks | Passed | Ruff lint and formatting; strict mypy across 59 source files |
| HTTP and message contracts | Passed | OpenAPI 3.1 validation for 26 paths, versioned JSON schemas, and response validation within onboarding/access tests |
| Django configuration | Passed | Fail-closed production settings, database-role binding, static asset collection, and migration drift check |
| Python dependency audit | No known advisories reported | All 34 locked runtime dependencies, checked with pip-audit 2.10.1 |
| Python security analysis | No medium/high findings reported | Bandit 1.9.4 on application and operational scripts; two low-severity subprocess diagnostics remain in the deployment-check utility |
| PHP standards and typing | Passed | WordPress Coding Standards 3.4.1 and PHPStan 2.2.13 at level 6, using PHP 8.3.6 |
| PHP signing protocol | Passed | Shared Python/PHP Ed25519 vector, tamper rejection, sealed key recovery, salt changes, decimal handling and acknowledgement rejection |
| PHP dependency advisory check | No matching advisories reported | Nine locked development dependencies compared with the official Packagist advisory feed using Composer's semantic version matcher |
| Browser dependency audit | No known advisories reported | Locked Playwright 1.62.1 test dependencies checked with npm |
| JavaScript syntax | Passed | Dashboard mutations and browser journey script |
| Report generation | Passed | Synthetic CSV audit produced CSV, HTML, PDF and JSON evidence, with one expected PI-001 finding and no data warnings; original inputs remained unchanged |
| PDF visual review | Passed | Rendered A4 example checked for readable typography, amounts, identity text and footer; DejaVu fonts embedded and distributed with their license |
| Plugin packaging | Passed | Two builds for the same HTTPS origin produced byte-identical ZIPs; manifest, SHA-256 and CycloneDX files generated |
| Infrastructure syntax | Passed within this scope | All HCL files parsed; 10 bootstrap and 96 application resource declarations have unique addresses; four workflow files and six Compose services parsed |
| Repository packaging | Passed | Relative documentation links resolve; action references use full commit hashes; archive excludes local secrets, state, dependency directories and generated data |

Coverage is an aggregate statement measurement, not branch coverage or a per-file guarantee. Management commands are included even when a command is exercised only by a later environment gate. The integration database was disposable and contained synthetic records only. The accompanying [machine-readable results](verification-results.json) record the final counts.

Composer's final direct audit request encountered a proxy TLS error. The advisory feed was retrieved over verified HTTPS with a separate standard client and its affected-version constraints were evaluated locally with Composer Semver; no locked version matched. The normal Composer audit remains a required CI check.

## Database boundary

The handoff integration harness used PostgreSQL 17 compiled to WebAssembly through PGlite 0.3.16. It exercised real SQL, constraints, FORCE RLS, migrations, immutable history and application transactions while explicitly switching to the non-owner runtime role. It did not prove network password authentication or independent concurrent native sessions.

Exactly two tests were skipped: direct runtime-role authentication/privilege checks and concurrent duplicate delivery across independent native connections. Both reside in `tests/test_native_concurrency.py`; the native PostgreSQL 17 CI job runs them without the PGlite skip flag. This is a mandatory gate before a connected pilot.

The suite also covers tenant isolation, scoped merchant invitations, revoked access, temporary support access, owner security notices, current notification authorization, stale and partial coverage, signature replay, OAuth refresh uncertainty, provider identity substitution, queue retries, terminal-work coverage invalidation, report expiry, erasure/retention, and deployment rollback/read-back behavior. AWS calls in these tests use deterministic substitutes; they do not prove an AWS deployment.

## Checks requiring the target environment

| Gate | Handoff status | Where to complete it |
| --- | --- | --- |
| Native PostgreSQL authentication/concurrency | Pending | Source checks workflow on native PostgreSQL 17 |
| Docker image and Compose health | Pending | Container job in Source checks |
| Desktop/mobile browser journeys | Pending | Playwright journey in the container job; screenshots retained as artifacts |
| WordPress/WooCommerce runtime matrix | Pending | WooCommerce compatibility workflow: 16 combinations of PHP, WordPress, WooCommerce and HPOS |
| Terraform provider validation and plan | Pending | Infrastructure job, followed by a reviewed plan in the selected AWS account |
| Trivy source/container/IaC checks | Pending | Source and release workflows |
| AWS bootstrap, Cognito MFA, SES feedback and signed rollout/rollback | Pending | Staging deployment and operations runbooks |
| Stripe App authorization, permissions and live connector compatibility | Pending | Isolated provider sandbox and pilot acceptance |
| Restore/erasure replay, sustained load, accessibility and independent security review | Pending | Measured release-gate exercises |

The handoff host did not provide Docker or a native WordPress database. Browser startup failed with an executable access error. Terraform execution was blocked by the host's approval policy; provider validation and planning did not complete. Trivy terminated with a host runtime failure before producing results. These attempts are recorded as unverified, not as passed scans. No access controls or checksum checks were disabled to obtain a result.

## Repeat the checks

From a clean checkout, install the frozen dependencies and run `make check`. With an isolated disposable PostgreSQL database ending in `_test`, run the [README's integration command](../README.md#development). Keep `LEDGERGUARD_PGLITE` unset for native acceptance. CI collects coverage and JUnit artifacts.

For PHP, run:

```sh
composer install --working-dir=plugins/ledgerguard-woocommerce --no-interaction --prefer-dist
composer audit --working-dir=plugins/ledgerguard-woocommerce --locked
plugins/ledgerguard-woocommerce/vendor/bin/phpcs --standard=plugins/ledgerguard-woocommerce/phpcs.xml.dist
plugins/ledgerguard-woocommerce/vendor/bin/phpstan analyse -c plugins/ledgerguard-woocommerce/phpstan.neon.dist --memory-limit=2G --debug --no-progress
php tests/php/protocol.php
```

For the browser journey, start the local Compose stack, seed a new `browser_demo` user, install with `npm ci --prefix tests/browser`, then run `npx --prefix tests/browser playwright install --with-deps chromium`. Set `LG_BROWSER_PASSWORD` to that synthetic user's password and run `node tests/browser/dashboard.mjs`. Use a fresh synthetic tenant for each run: the journey acknowledges a finding and changes its organization's retention setting.

Record the repository commit and signed artifact digest against each completed staging gate. Uploading source to GitHub does not satisfy the [pilot, beta or GA release gates](release-gates.md).
