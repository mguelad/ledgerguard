# Security model

## Access

| Role | Scope |
| --- | --- |
| Owner | Organization administration, members, support grants, deletion and operational actions |
| Admin | Store/connectors, policies, alerts and investigation |
| Analyst | Findings and report requests |
| Viewer | Organization evidence and report reads |
| Merchant | Read access to one explicitly assigned store and its findings |
| Support | Customer-approved read access, at most two hours, audited per request |

Django staff status alone grants no tenant access. Support writes fail the same role checks as other unauthorized writes. Sensitive changes require authentication within ten minutes. Membership is checked on every request; queued finding email rechecks active membership and current alert switches. Security-sensitive connector, membership, support, export, alert-recipient and deletion changes create separate owner-notification intents in the audit transaction; delivery rechecks the current owner and route. Organization-wide email is unavailable to merchant-scoped members.

Cognito OIDC uses PKCE, one-use session state and nonce, issuer/audience/signature checks, verified email and recent `auth_time`. The user-pool requires MFA. Session cookies are Secure, HttpOnly and SameSite=Lax; idle timeout is thirty minutes and absolute lifetime eight hours. CSRF protects browser mutations, including logout. A restrictive CSP allows only bundled scripts/styles.

## Data boundaries

Tenant context is transaction-local and cannot change inside a nested scope. RLS denies unscoped business reads/writes. Composite foreign keys enforce same-tenant relationships. Immutable history rejects database UPDATE even for maintenance; runtime cannot DELETE history. Maintenance deletion is explicit, verified and separate from online credentials.

Credential blobs use AES-256-GCM envelope encryption. AAD binds organization, connector, environment and credential type; KMS encrypts the random data key with the same context. Local development uses a separate random key and is not a production substitute. Provider tokens, webhook secrets and key material never appear in safe operational logs.

Signed Woo bodies require a five-minute time window, strict schema and bounded input: 1 MiB, depth twelve and at most one hundred facts. Replay IDs are retained for at least twenty-four hours. The HMAC Stripe boundary verifies raw bytes before minimal receipt persistence; account and test/live namespace must agree.

## Retention and deletion

| Data | Policy |
| --- | --- |
| Normalized facts and resolved findings | Organization choice: 90, 180 or 400 days |
| Unresolved findings and referenced facts | Retained while investigation remains open |
| Audit | 400 days |
| Reports | At most one day; signed URL lifetime sixty seconds |
| Completed receipts/scan acknowledgments/work | Two-day cleanup; replay floor remains twenty-four hours |
| Ordinary logs | Thirty days; security logs ninety days |
| RDS backups | Thirty-five-day PITR retention |
| Raw webhook bodies | Never persisted |
| Offline source CSVs | Read in place; originals remain the operator's responsibility and must be removed within the agreed import retention |
| Connector credentials | Forgotten immediately on disconnect or deletion request |

Deletion stops intake, destroys stored credentials, erases tenant rows and report object versions, verifies no tenant rows remain, and records an opaque receipt outside RDS. Backups age out; isolated restore must replay the erasure ledger before service resumes. Directory receipts contain an opaque ID and digest, not source facts. Account identities can be shared across organizations and are not blindly deleted with one tenant.

ASVS 5.0 Level 2, independent penetration testing, access review and legal agreements remain explicit [release gates](release-gates.md).
