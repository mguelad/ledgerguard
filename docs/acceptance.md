# AWS and provider acceptance tooling

AWS is the implementation target. These commands require initialized accounts and an isolated test store. They do not provision resources, publish an App or approve a launch. Keep output in ignored `var/acceptance/` and the access-controlled release evidence store, not public GitHub issues. Record commit, signed artifact digests, operator and test start/end alongside each result.

## Stripe App contract

```sh
uv run python -m scripts.build_stripe_app --app-id YOUR_UNIQUE_APP_ID --origin https://YOUR_ORIGIN
uv run python -m scripts.build_stripe_app --app-id YOUR_UNIQUE_APP_ID --origin https://YOUR_ORIGIN --check dist/stripe-app.json
```

Supply your actual registered ID and exact HTTPS origin. The generator uses the repository version, four reviewed read permissions, OAuth and one callback. The release signs `stripe-app.json` and includes it in provenance. Verify it with the release-workflow identity and its Sigstore bundle, then compare it against the manifest actually uploaded/installed in Stripe. This check cannot introspect a merchant's installed permission grant.

Register the App and exercise browser consent through External test. See Stripe's [manifest](https://docs.stripe.com/stripe-apps/reference/app-manifest), [permissions](https://docs.stripe.com/stripe-apps/reference/permissions), and [OAuth](https://docs.stripe.com/stripe-apps/api-authentication/oauth) contracts. Stripe controls ID availability, upload acceptance and publication. Managed Sandbox support is not advertised; legacy test-mode and managed-sandbox credentials are not interchangeable.

## AWS configuration preflight

After reviewed Terraform applies, signed deployment, database initialization and SES configuration:

```sh
mkdir -p var/acceptance
terraform -chdir=infra/terraform output -json acceptance_config > var/acceptance/aws-config.json
uv run python -m scripts.check_aws_readiness \
  --config var/acceptance/aws-config.json --account-id YOUR_12_DIGIT_ACCOUNT \
  --environment staging --image 'YOUR_ECR_REPOSITORY@sha256:VERIFIED_DIGEST'
```

Export **only** `acceptance_config`; full Terraform outputs contain secrets. Use short-lived operator credentials with read-only access to the selected resources. Required APIs: STS GetCallerIdentity; RDS DescribeDBInstances/DescribeDBParameters; KMS DescribeKey/GetKeyRotationStatus; S3 GetBucketPublicAccessBlock/GetEncryptionConfiguration/GetBucketVersioning; Cognito GetUserPoolMfaConfig; SQS GetQueueAttributes; ECS DescribeServices/DescribeTaskDefinition; Scheduler GetSchedule; SESv2 GetAccount/GetEmailIdentity. An access error fails, rather than skips, a check. The deployment role is not automatically granted diagnostic permissions.

The checker verifies the expected account before resource access, eu-west-1 and environment-bound names. Its 24 checks/groups cover private encrypted PostgreSQL 17, forced TLS, Multi-AZ, deletion protection, backup retention/current PITR point; KMS; private/versioned report and erasure buckets; required TOTP configuration; five queues' encryption/redrive and empty DLQs; six ECS services and both schedules on the expected image; SES sending access/domain verification. Output contains fixed check names/statuses, timestamp, environment and image—not raw AWS responses or secrets.

Passing proves only this configuration snapshot. It does **not** prove actual MFA login, runtime database privileges, email/feedback delivery, WAF behavior, certificate validity, alarm delivery, egress restrictions, throughput or restore success. PITR recency is **not measured RPO**. All reports retain `production_accepted: false`.

## Installed Stripe test-resource reads and refresh

Prepare one synthetic PaymentIntent, charge, refund, payment-mode Checkout Session and associated event in a dedicated Stripe test account using its normal test tools. This checker never creates/refunds payments. Connect the account through LedgerGuard's browser OAuth flow. From the corresponding development/dev/staging runtime, under the application database role:

```sh
uv run python manage.py check_stripe_readiness \
  --organization TENANT_UUID --installation STRIPE_INSTALLATION_UUID \
  --confirm-test-account acct_TEST_ACCOUNT \
  --payment-intent-id pi_TEST --charge-id ch_TEST --refund-id re_TEST \
  --event-id evt_TEST --checkout-session-id cs_test_TEST --rotate-token
```

This requires a matching active tenant, installation, account and test namespace; production is refused. It uses stored encrypted credentials, optionally forces/persists OAuth refresh, and performs ten bounded GETs (list and exact retrieval for each resource). It validates object identity/test mode and records only resource classes and status—not IDs, source bodies or tokens. Expired tokens are refreshed even without the force flag. An ambiguous refresh or identity/mode conflict suspends the installation; reauthorize through the browser. A transient GET failure preserves a successfully rotated pair for retry.

Passing does not prove browser consent security, absence of additional installed permissions, webhook secret rotation/deduplication/outage recovery, poll/backfill parity or full Woo-to-Stripe reconciliation. JSON explicitly lists unverified provider gates. Run those scenarios in staging and retain actual evidence; mocks cannot replace them.

## Recovery and remaining environment work

The [restore runbook](runbooks/database-recovery.md) includes a target-bound dry run and idempotent erasure replay with read-only external-ledger access. Native PostgreSQL tests simulate restored tenant data and verify original receipt preservation; they are not an RDS snapshot/PITR drill. Measure recovery from incident declaration through safe canary reopening, and data loss against a committed synthetic canary. Keep ingress, jobs and alerts isolated until verification completes.

Still required with AWS/provider access: signed release → staging → rollback; actual restore/erasure replay with RPO/RTO; SQS/SES/provider outage and poison-message drills; representative 500-store/5M-fact load and 20-webhook/sec bursts with server-side percentiles; sustained Woo/Stripe pilot; keyboard/screen-reader and independent security reviews. Customer consent, privacy/legal decisions, account ownership and on-call responsibility require the product owner and relevant reviewers. None is marked complete by these tools.
