from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_tenantdirectory_erased_at_and_more")]
    operations = [
        migrations.RunSQL(
            """
        REVOKE UPDATE,DELETE ON accounts_auditlog,ingestion_observation,
            findings_findingevent,findings_reconciliationrun FROM ledgerguard_app;
        REVOKE UPDATE ON accounts_auditlog,ingestion_observation,
            findings_findingevent,findings_reconciliationrun FROM ledgerguard_maintenance;
        REVOKE ALL ON django_migrations FROM ledgerguard_app,ledgerguard_maintenance;
        ALTER TABLE accounts_membership ADD CONSTRAINT membership_store_tenant_fk
            FOREIGN KEY (store_id,organization_id) REFERENCES connectors_store(id,organization_id)
            DEFERRABLE INITIALLY DEFERRED;
        """,
            reverse_sql=migrations.RunSQL.noop,
        )
    ]
