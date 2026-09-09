# Release gates

A source archive is not evidence that a configured service meets production objectives. Keep an evidence record for each gate with environment, commit, artifact digest, date, operator, expected outcome and actual result.

## Before a connected pilot

- Native PostgreSQL 17 isolation, actual role authentication, migration rollback compatibility and concurrent duplicate/retry tests pass.
- Stripe sandbox App OAuth, permissions, rotation uncertainty, webhook recovery and overlap parity pass. App permissions contain no financial writes.
- Supported WordPress/Woo/Stripe-gateway combinations pass with HPOS both enabled and disabled, including refunds, deletions, retries, mode changes and signature recovery.
- Synthetic browser journeys pass at desktop/mobile sizes; keyboard and screen-reader review covers sign-in, pairing, evidence and finding actions.
- Signed immutable image/plugin builds, dependency/container/IaC security checks and staging rollout/rollback pass.
- Provider outage, alert storm, notification outage, poison-message replay, tenant deletion and an isolated backup restore are exercised.
- Pilot customers approve data fields, residency, retention, support access and the required agreements. ADR-010 remains Proposed until residency/procurement acceptance is recorded.

## Before paid beta and GA

| Objective | Required measured evidence |
| --- | --- |
| Webhook response | p95 <500 ms, p99 <1 second excluding network; include failure/duplicate cases |
| Detection at GA | p95 <5 minutes after rule grace under representative traffic |
| Availability at GA | 99.9% monthly public ingest and dashboard; publish excluded planned maintenance |
| Recovery | RPO ≤5 minutes, RTO ≤4 hours beta / ≤1 hour GA, tested restore and erasure replay |
| Scale | 500 stores, 5 million facts/month, 20 webhook requests/second burst; include shared-account and large-store cases |
| Accessibility | WCAG 2.2 AA critical journeys, keyboard and screen reader |
| Security | ASVS 5.0 Level 2 target; independent penetration test before GA; zero open critical findings |
| Maintainability | ≥80% changed-code coverage, strict typing, ADRs and reproducible local stack |

The default Terraform service counts are zero until secrets, database roles, migrations and signed images are prepared. Set GA flags only after evidence passes; flags do not themselves prove compliance. Preserve false-positive labels and review supported methods before expanding scope. Slack/Teams, billing, subscriptions, disputes, payouts, FX and automatic remediation are outside v1.
