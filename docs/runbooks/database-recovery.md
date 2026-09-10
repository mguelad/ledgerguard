# Database Recovery

1. Keep ingress, schedulers, notifications and workers stopped. Record incident declaration time and inspect RDS LatestRestorableTime; record the expected data-loss interval.
2. Restore to a **new private RDS instance**. Verify encryption, parameter group, TLS hostname, database roles and FORCE RLS, then use separate reviewed connection secrets. Do not point public services at it yet.
3. Under the maintenance role, run `python manage.py replay_erasures --dry-run --expected-database-host RESTORED_RDS_HOST --expected-database-name ledgerguard`. Grant temporary `s3:ListBucket` (prefix `erasures/`) and `s3:GetObject` access to the external deletion ledger, and `kms:Decrypt` for its key. Do not use an older restored copy of the external ledger.
4. Review the target and count, then replace `--dry-run` with `--confirm-isolated-restore`. The host/name must match the configured database; this check does not prove network isolation, which the operator must verify. Replay preserves the original erasure digest/date and never writes the external ledger. It still needs the existing report-version deletion permissions to remove restored reports. Do not rely on the older in-database tenant directory. A malformed receipt or any error blocks reopening traffic; a partially completed replay can be rerun safely.
5. Verify erased tenants have no rows/reports, run migrations if compatible, and test synthetic isolation plus a known evidence case. Perform overlap repair with alerts disabled, then restore one canary.
6. Record actual RPO against a committed synthetic canary and RTO through safe canary reopening. Do not claim the design targets if the drill missed them. Preserve the old instance until rollback is no longer needed.

The native PostgreSQL replay regression is not a successful AWS backup/restore drill. Retain real timestamps, database IDs, signed image digest, verification results and reviewer approval in the restricted acceptance evidence store.
