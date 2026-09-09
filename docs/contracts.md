# HTTP and queue contracts

`contracts/openapi.json` describes all 26 public API paths, including OAuth callbacks and signed ingestion. JSON Schemas under `contracts/schemas` pin the Woo wire format to 1.0 and queue references to version 1. Unknown fields, versions, duplicate JSON keys, non-finite numbers and over-depth input are rejected. JSON bodies are limited to 1 MiB and nesting to 12 levels. Woo batches contain at most 100 facts. Timestamps include UTC; transport timestamps end in `Z`.

Run `python scripts/build_contracts.py` from an installed development checkout to regenerate OpenAPI after an endpoint change, review the diff, then run `python scripts/check_contracts.py`. The onboarding and access tests validate real response bodies against that contract. The portable Ed25519 fixture is verified independently by PHP and Python.

## Browser requests

Authentication uses a secure, HTTP-only SameSite=Lax Django session in cloud environments. Cognito authorization code flow uses PKCE, nonce, one-use state and verified ID-token claims. All browser POST requests require a CSRF token; sensitive operations require a sign-in within the last ten minutes. Sessions expire after eight hours, with a 30-minute idle limit.

Mutations also require `Idempotency-Key`: 16–128 letters, digits, underscores or hyphens. Scope is organization, user, route and key for 24 hours. Same key/body recovers the original accepted JSON response; a different body returns 409. Pairing codes and invitation results are sealed at rest. Permissions and active-organization status are checked before replay. Creation of an organization is scoped to the authenticated principal before tenant context exists. Invitation acceptance consumes the invitation token instead of using this header.

Store policy, organization policy, finding actions, connector disconnect and webhook-secret rotation require `If-Match` with the current `lock_version`, optionally quoted. A missing version returns 428; a stale version returns 409. A changed request body needs a new idempotency key. An interrupted connection should retry the same key and body.

| Response | Meaning |
| --- | --- |
| 200 / 201 | Accepted result or recovered replay; inspect result status for link conflicts |
| 202 | Report generation or tenant deletion is durably queued |
| 302 | OAuth navigation or an authenticated, 60-second private report URL |
| 400 / 422 | Invalid envelope, fields or bounded policy |
| 401 / 403 | Authentication, role or recent sign-in requirement |
| 404 | Absent resource, expired artifact or tenant-inaccessible identity |
| 409 / 428 | Conflicting replay, source revision, optimistic version or onboarding prerequisite |
| 413 / 429 | Body limit or quota; reduce work or back off |
| 503 | Provider/storage unavailable or a connector requiring reauthorization |

Application errors use `application/problem+json` with `type`, `title`, `status`, `code`, `detail` and `correlation_id`. Framework-level CSRF and method rejections may return HTML. Do not parse human-readable error wording as a retry policy.

## Woo protocol

`X-LedgerGuard-Signature` is a Base64 Ed25519 signature. `X-LedgerGuard-Key-ID` selects a registered public key. Sign the exact UTF-8 body bytes with the following newline-separated base, without a final newline:

```text
v1
<installation UUID, or literal claim for initial registration>
<request UUID>
<sent_at UTC timestamp>
<lowercase SHA-256 of exact body bytes>
```

A five-minute clock window limits replay. Request IDs deduplicate byte-identical requests; conflicting bytes return 409. Expired transport timestamps may be renewed with a new request ID. Semantic scan/page identities still deduplicate unchanged normalized facts. A page number, scan ID, fixed coverage window and final-page marker establish contiguous completeness. Per-record rejections prevent coverage advance and report a safe error code. A heartbeat cannot advance order/refund coverage.

Pairing expires in ten minutes. Key rotation proves possession of an active key and gives the prior key a bounded 24-hour overlap. Rotation retries cannot extend that expiry. Disconnect rejects further intake immediately. A WordPress salt change requires re-pairing; the service cannot recover the private key.

## Stripe and work queues

Stripe signatures cover the raw received body, with a five-minute timestamp tolerance and an explicitly expiring previous secret during rotation. Persisted receipts contain routing/event/object identifiers and a body digest, never raw event data. A worker fetches the current allowlisted resource before updating projections. OAuth exchanges are not automatically retried after an uncertain response.

SQS carries only schema version, work UUID, tenant UUID, task type, resource UUID, attempt context and W3C trace context. The database outbox is authoritative. Queue messages cannot supply amounts, statuses or evidence. Finding and owner-security notification work references immutable events rather than email content. A locked work reference serializes effects; terminal work is deduplicated. Failed work uses jittered exponential backoff and stops after eight attempts or a permanent connector error. Reviewed replay creates a new work reference and an audit event; it does not alter historical evidence.
