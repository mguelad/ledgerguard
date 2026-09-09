# Engineering documentation

Start with the repository [README](../README.md) for local setup and the code map.

| Document | Purpose |
| --- | --- |
| [Architecture](architecture.md) | System boundaries, processing and deployment design |
| [Requirement traceability](traceability.md) | Handoff requirements mapped to implementation and verification |
| [Architecture decisions](adr/README.md) | Accepted decisions and explicit refinements |
| [Security model](security-model.md) | Tenant isolation, credentials, authorization and data handling |
| [API and message contracts](contracts.md) | HTTP interfaces, signing, idempotency and queues |
| [Connectors](connectors.md) | Stripe App and WooCommerce setup and operational boundaries |
| [Offline audit](csv-audit.md) | CSV input contracts and report usage |
| [Deployment](deployment.md) | Bootstrap, configuration, migrations and release procedure |
| [Runbooks](runbooks/README.md) | Recovery, rotation, deletion and incident response |
| [Verification record](verification.md) | Measured checks and outstanding environment validation |
| [Release gates](release-gates.md) | Pilot, beta and GA acceptance criteria |
| [Static analysis review](static-review.md) | Scope and ownership of analysis configuration |

Changes and dependency licensing are recorded in [CHANGELOG](../CHANGELOG.md) and [third-party notices](../THIRD_PARTY_NOTICES.md).
