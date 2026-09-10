# Deployment

Run a connected pilot in staging before using live merchant data. [Release gates](release-gates.md) define acceptance evidence; [verification](verification.md) records what has actually been executed. Terraform creates billable AWS resources only when an operator applies it. Source upload and pull-request checks do not create cloud resources.

## Repository and accounts

Use separate AWS accounts for `dev`, `staging` and `production`, all in `eu-west-1`. Cloud `dev` uses the same secure authentication/TLS settings as production. `development` is the local Compose setting. Use an owned DNS name and Route 53 public zone. Configure AWS CLI through IAM Identity Center or another short-lived administrator session; do not put access keys in the repository.

Create GitHub environments with those exact names. Limit deployment branches to protected `main`, require an independent reviewer, prevent self-review and protect workflow/IaC changes. Require the Source checks and WooCommerce compatibility workflows before merging. Enable private vulnerability reporting and dependency update review. Install or reuse an AWS OIDC provider for `https://token.actions.githubusercontent.com` with audience `sts.amazonaws.com`; the Terraform deploy role trusts only this repository's selected environment.

## Establish protected state

The bootstrap module creates a private, versioned KMS-encrypted state bucket, three empty secret containers and an encrypted operations topic. Subscribe the actual operations destination and verify receipt of a test alarm before enabling a pilot; no recipient is embedded in source.

```sh
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap plan -var='aws_account_id=YOUR_ACCOUNT_ID' -var='environment=staging' -out=bootstrap.plan
terraform -chdir=infra/bootstrap apply bootstrap.plan
terraform -chdir=infra/bootstrap output
```

Move bootstrap state into its new bucket under `bootstrap/terraform.tfstate` using an S3 backend block and `terraform init -migrate-state`. Use a separate state key `application/terraform.tfstate` for the application module, `encrypt=true`, `region=eu-west-1`, its KMS key ARN and `use_lockfile=true`. Restrict both bucket and KMS permissions to the infrastructure administrators. Never commit local state, plans or real variable files. Keep the initial bootstrap state securely until migration is verified.

Copy `infra/terraform/terraform.tfvars.example` to a private variable file. Supply account, environment, domain/zone, repository, OIDC provider, the three bootstrap secret ARNs and operations topic ARN. First apply with zero service counts and disabled schedules. For this initial disabled configuration only, the placeholder digest in the example is not pulled or trusted as a release.

```sh
terraform -chdir=infra/terraform init -backend-config=/secure/path/backend.hcl
terraform -chdir=infra/terraform plan -var-file=/secure/path/staging.tfvars -out=/secure/path/staging.plan
terraform -chdir=infra/terraform apply /secure/path/staging.plan
```

Review the plan, IAM grants, network rules, database protection and costs before applying. The baseline includes two NAT gateways, Multi-AZ RDS and private Fargate tasks. It deliberately has no public database, SSH host, inbound Woo endpoint or application-shell access.

## Build the first release

Set these GitHub environment variables from the Terraform outputs/account configuration:

| Variable | Value |
| --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | ARN of `ledgerguard-<environment>-github-deploy` |
| `ECR_REPOSITORY` | Full ECR repository URL, without tag or digest |
| `PUBLIC_ORIGIN` | Exact deployed HTTPS origin, without a path |
| `STRIPE_APP_ID` | Operator-selected unique Stripe App ID for this environment |

Run **Build and sign release** from protected `main`. It executes source/security/container and WordPress matrix checks before building, scanning and signing. The release artifact contains the destination-bound plugin ZIP, checksums, manifest, Sigstore bundle and immutable image reference. The container includes an SBOM and build provenance; the signed digest binds the built image index. Do not use an unsigned image or override a failed release check.

## Initialize database roles and secrets

The first deployment requires an administrator with permission to register/run the dedicated bootstrap task and pass its role. The GitHub deploy role intentionally cannot pass this privileged bootstrap role. Verify the image signature with the release workflow identity first, using the same `cosign verify` arguments as `deploy.yml`.

```sh
uv sync --frozen
uv run python scripts/deploy.py --environment staging \
  --image 'YOUR_ECR_REPOSITORY@sha256:VERIFIED_DIGEST' --initialize
```

This runs a private bootstrap task, then a migration task, then the API/workers and schedules. Bootstrap retrieves the RDS-managed master secret, creates three non-superuser/non-bypass roles, sets default grants, generates database/session secrets and records the Cognito client secret. No secret value is printed. The bootstrap task does not require an existing application secret version. A retry reuses the stored database passwords; secret values are saved before database changes to make partial initialization recoverable. Do not use it as an unattended credential-rotation mechanism.

The `ledgerguard_migrator` role owns application tables. `ledgerguard_app` cannot assume it and cannot update/delete immutable evidence. `ledgerguard_maintenance` can execute the reviewed erasure/retention path under tenant scope. Migrations apply FORCE RLS, composite tenant references and append-only history protections. The master secret is unavailable to runtime and normal deployment roles.

Immediately update the private Terraform variables to the verified image digest and desired counts (at least one per worker class, two API tasks for production), then plan/apply again. This sets the autoscaling minimums and enables scaling. Scaling is suspended while the configured counts are zero. Keep `image_digest_uri` synchronized with each release before subsequent infrastructure applies: Terraform must not restore an old task revision. Review schedule targets as well as service definitions in every plan.

## Complete service configuration

Populate the application's existing Secrets Manager JSON with separate Stripe App client IDs and developer account keys for test/live. Preserve its generated `DATABASE_URL`, `DJANGO_SECRET_KEY` and `COGNITO_CLIENT_SECRET`. These developer keys perform only the App token exchange; merchants authorize through OAuth. Re-deploy the same verified digest to refresh ECS-injected secrets. [Connector setup](connectors.md) specifies App permissions, callbacks, webhook destinations and plugin installation.

Verify SES domain/DKIM records, request production sending access if the account remains in the sandbox, and exercise Send/Delivery/Bounce/Complaint feedback. Create a Cognito user, complete required TOTP enrollment, sign in, create a tenant and pair an isolated test store. Apply the onboarding gates before enabling store and organization alerts. No live source credentials or financial actions are part of automated repository checks.

Run the [AWS configuration preflight and Stripe test-resource checks](acceptance.md) after initialization. The preflight uses only describe/get operations and never retrieves secret values. Missing permissions, disabled services, old images and incomplete settings fail the check; passing does not replace end-to-end acceptance.

Database connections verify both the server certificate chain and hostname using the checked-in AWS regional RDS CA bundle. Verify its recorded SHA-256 and refresh it through review before CA expiry/rotation. Public certificates are not private keys. The application logs omit arbitrary strings, raw requests and credentials; ALB raw URL access logs are intentionally disabled to avoid retaining OAuth codes.

## Routine releases and recovery

Use **Deploy verified release** with the full digest from a successful release. The workflow verifies the signer identity before migrations. Migrations must exit zero before rollout; the script checks the actual requested ECS task revisions after stabilization, so an automatic circuit-breaker rollback cannot be reported as success. Both scheduled operations move to the same digest.

If a rollout fails, the script restores the previous service definitions/counts and any changed schedule targets, then verifies stabilization. It never reverses a database migration automatically. All schema changes must remain compatible with the preceding application version; destructive contraction requires its own reviewed migration after the compatibility window. Use the [database recovery](runbooks/database-recovery.md), [credential incident](runbooks/credential-incident.md) and [plugin rollback](runbooks/plugin-rollback.md) runbooks for the corresponding incident.

The maintenance task records tenant erasures in a separate encrypted S3 ledger before committing database erasure. For a point-in-time restore, block traffic and replay this ledger into the restored database before enabling users or workers. Rotate compromised credentials, verify RLS with the native test suite and measure actual RPO/RTO. Backups age out after 35 days; a provisioned backup setting is not a successful recovery drill.
