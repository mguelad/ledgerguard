# Operations

Keep credentials and source payloads out of incident tickets. Record UTC time, environment, image digest, opaque resource IDs, error codes and actions. An incident owner determines when evidence supports reopening traffic. Customer communications require an authorized operator.

| Situation | Runbook |
| --- | --- |
| Connector stale / provider outage | [Connector recovery](connector-recovery.md) |
| Stripe token or webhook-secret compromise | [Credential incident](credential-incident.md) |
| Woo key compromise / re-pair | [Credential incident](credential-incident.md) |
| Cross-tenant access or secret exposure | [Security incident](security-incident.md) |
| False-positive storm / kill switch | [Alert storm](alert-storm.md) |
| SQS poison message / DLQ | [Queue recovery](queue-recovery.md) |
| RDS failover / PITR | [Database recovery](database-recovery.md) |
| Export / deletion | [Tenant deletion](tenant-deletion.md) |
| Incompatible Woo release | [Plugin rollback](plugin-rollback.md) |
| Notification outage | [Notification recovery](notification-recovery.md) |
