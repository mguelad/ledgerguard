# Changelog

## 0.1.0 — 2026-09-09

Initial implementation for pilot verification.

- Implemented the ten payment integrity rules, conservative identity matching, coverage and grace gates, immutable evidence, and finding lifecycle.
- Added Stripe App OAuth and read-only ingestion, the signed outbound WooCommerce connector, and resumable scan contracts.
- Added organization and store access, MFA integration, invitations, alert controls, temporary audited support access, and evidence retention.
- Added the investigation dashboard, finding and security-sensitive owner notifications, CSV/HTML/PDF reporting, and an offline audit command.
- Added PostgreSQL tenant isolation, transactional queues, recovery and erasure operations, the local Compose stack, and AWS deployment definitions.
- Added golden fixtures, property and integration tests, native database and plugin matrix gates, signed release workflows, runbooks, and requirement traceability.
- Added fail-closed deployed configuration, process/database-role binding, provider identity validation, terminal-work coverage invalidation, and deployment read-back verification.

This version is a source handoff. See the [verification record](docs/verification.md) and [release gates](docs/release-gates.md) before connecting a pilot or launching a paid service.
