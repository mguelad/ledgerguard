from django.db import migrations

APPS = ("accounts", "connectors", "ingestion", "findings", "reporting")
IMMUTABLE = ("accounts_auditlog", "ingestion_observation", "findings_findingevent", "findings_reconciliationrun")


def install(apps, schema_editor):
    q = schema_editor.quote_name
    tenant_models = [
        m
        for label in APPS
        for m in apps.get_app_config(label).get_models()
        if any(f.name == "organization_id" for f in m._meta.fields)
        and m._meta.model_name not in {"resourcelocator", "pairinglocator"}
    ]
    for model in tenant_models:
        table = q(model._meta.db_table)
        schema_editor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        schema_editor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        expr = "organization_id = NULLIF(current_setting('app.organization_id',true),'')::uuid"
        schema_editor.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({expr}) WITH CHECK ({expr})")
        if model._meta.model_name == "membership":
            schema_editor.execute(
                f"CREATE POLICY own_membership ON {table} FOR SELECT USING (NULLIF(current_setting('app.organization_id',true),'') IS NULL AND user_id = NULLIF(current_setting('app.user_id',true),'')::bigint)"
            )
        schema_editor.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {q(model._meta.db_table + '_tenant_identity')} UNIQUE (id,organization_id)"
        )
    for model in tenant_models:
        for field in model._meta.fields:
            if field.many_to_one or field.one_to_one:
                parent = field.remote_field.model
                if parent in tenant_models:
                    name = (model._meta.db_table + "_" + field.column + "_tenant_fk")[:63]
                    schema_editor.execute(
                        f"ALTER TABLE {q(model._meta.db_table)} ADD CONSTRAINT {q(name)} FOREIGN KEY ({q(field.column)},organization_id) REFERENCES {q(parent._meta.db_table)} (id,organization_id) DEFERRABLE INITIALLY DEFERRED"
                    )
    schema_editor.execute(
        """CREATE FUNCTION ledgerguard_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable_record' USING ERRCODE='42501'; END $$"""
    )
    for table in IMMUTABLE:
        schema_editor.execute(
            f"CREATE TRIGGER immutable_update BEFORE UPDATE ON {q(table)} FOR EACH ROW EXECUTE FUNCTION ledgerguard_immutable()"
        )
    schema_editor.execute("CREATE INDEX observation_created_brin ON ingestion_observation USING BRIN(created_at)")
    schema_editor.execute(
        "ALTER TABLE accounts_organization ADD CONSTRAINT organization_matches_tenant CHECK (id=organization_id)"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("connectors", "0001_initial"),
        ("ingestion", "0003_batchpage"),
        ("findings", "0002_emaildelivery"),
        ("reporting", "0001_initial"),
    ]
    operations = [migrations.RunPython(install, reverse_code=migrations.RunPython.noop)]
