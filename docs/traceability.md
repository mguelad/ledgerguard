# Design traceability

Baseline: LedgerGuard Implementation Design v1.0 Engineer Handoff, 46 pages. Requirements below map to executable code and verification. The private source PDF is not redistributed in the repository.

| Requirement | Implementation | Evidence |
| --- | --- | --- |
| FR-001 Organization isolation | Tenant scope, RLS migration, composite tenant FKs | Database isolation and browser authorization tests |
| FR-002 Stripe authorization | `connectors/stripe.py`, encrypted OAuth credentials | OAuth/read-client and onboarding tests; sandbox gate |
| FR-003 Woo pairing | Signed claim, code hash, WordPress nonce/capability/TLS | Shared signature fixture and onboarding test; native WP gate |
| FR-004 Hybrid ingestion | Webhook receipts, current GET, Woo scans, periodic repair | Projection/scan/retry tests; provider parity gate |
| FR-005 Schema validation | Versioned JSON Schemas, bounded parser, strict serializers | Contract/schema rejection tests |
| FR-006 Idempotency | Receipts, semantic pages, observations, outbox and mutation keys | Replay/content-conflict/transaction tests |
| FR-007 Reconciliation | Pure matching and PI-001–PI-010 | Rule/property/golden tests |
| FR-008 Evidence | Immutable observations, versioned runs, finding events | Evidence provenance and append-only tests |
| FR-009 Lifecycle | Grace, suppression, consecutive clean runs and reopening | Lifecycle/concurrency tests |
| FR-010 Notifications | Transactional finding/security intents, tracked digest/delivery, SES feedback | Owner/revocation/kill-switch/bounce/retry tests |
| FR-011 Exports | CSV/HTML/PDF and offline evidence JSON | Formula injection, escaping, format/expiry tests |
| FR-012 Deletion | Immediate disconnect, maintenance erasure and external ledger | Tenant/report erasure tests; restore gate |
| FR-013 Support | Customer grant ≤2 hours, read-only, request audit | Access expiry/revocation tests |
| FR-014 Rule controls | Bounded store policy, dry-run gate, rule and alert switches | Policy/optimistic-version tests |

NFR-001–NFR-008 map to [release gates](release-gates.md). Timing, availability, recovery, scale and independent security claims require environment evidence. Local coverage alone is insufficient.

Architectural decisions ADR-001–ADR-012 preserve the original statuses. [ADR-013](adr/013.md) records concrete protocol refinements, scheduler interval, refund inventory, compensated-duplicate behavior and external commit boundaries. The implementation does not silently convert the Proposed AWS decision into procurement approval.
