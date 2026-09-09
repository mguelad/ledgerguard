from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_invitation_store_id")]
    operations = [
        migrations.RunSQL(
            """
    ALTER TABLE accounts_invitation ADD CONSTRAINT invitation_store_tenant_fk FOREIGN KEY (store_id, organization_id) REFERENCES connectors_store(id, organization_id) DEFERRABLE INITIALLY DEFERRED;
    ALTER TABLE accounts_membership ADD CONSTRAINT member_role_scope CHECK ((role='merchant' AND store_id IS NOT NULL) OR (role IN ('owner','admin','analyst','viewer') AND store_id IS NULL));
    ALTER TABLE accounts_invitation ADD CONSTRAINT invitation_role_scope CHECK ((role='merchant' AND store_id IS NOT NULL) OR (role IN ('admin','analyst','viewer') AND store_id IS NULL));
    """,
            """ALTER TABLE accounts_invitation DROP CONSTRAINT invitation_role_scope, DROP CONSTRAINT invitation_store_tenant_fk; ALTER TABLE accounts_membership DROP CONSTRAINT member_role_scope;""",
        )
    ]
