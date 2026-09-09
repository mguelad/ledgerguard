"""Initialize restricted database roles from a dedicated, private bootstrap task.

Secrets are written before database changes, so an interrupted first run can resume
with the same credentials. This task runs only under the bootstrap administrator;
the application and deployment role cannot assume its database privileges.
"""

import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import boto3
import psycopg
from botocore.exceptions import ClientError
from psycopg import sql

ROLES = {"application": "ledgerguard_app", "migrator": "ledgerguard_migrator", "maintenance": "ledgerguard_maintenance"}


def secret_value(client, arn):
    try:
        return client.get_secret_value(SecretId=arn)["SecretString"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        # Distinguish an empty, existing container from an incorrect ARN.
        client.describe_secret(SecretId=arn)
        return None


def main() -> None:
    client = boto3.client("secretsmanager", region_name="eu-west-1")
    master = json.loads(client.get_secret_value(SecretId=os.environ["DATABASE_MASTER_SECRET_ARN"])["SecretString"])
    host = os.environ["DATABASE_HOST"]
    arns = {name: os.environ[name.upper() + "_SECRET_ARN"] for name in ROLES}
    app = json.loads(secret_value(client, arns["application"]) or "{}")
    app.setdefault("DJANGO_SECRET_KEY", secrets.token_urlsafe(64))
    for key in [
        "STRIPE_DEVELOPER_TEST_KEY",
        "STRIPE_DEVELOPER_LIVE_KEY",
        "STRIPE_TEST_CLIENT_ID",
        "STRIPE_LIVE_CLIENT_ID",
    ]:
        app.setdefault(key, "")
    cognito = boto3.client("cognito-idp", region_name="eu-west-1")
    pool_client = cognito.describe_user_pool_client(
        UserPoolId=os.environ["COGNITO_POOL_ID"], ClientId=os.environ["COGNITO_CLIENT_ID"]
    )["UserPoolClient"]
    app["COGNITO_CLIENT_SECRET"] = pool_client["ClientSecret"]
    passwords = {}
    for name, role in ROLES.items():
        existing = app.get("DATABASE_URL") if name == "application" else secret_value(client, arns[name])
        if existing:
            parsed = urlparse(existing)
            if (
                parsed.scheme != "postgresql"
                or parsed.hostname != host
                or parsed.path != "/ledgerguard"
                or unquote(parsed.username or "") != role
            ):
                raise ValueError("Existing database secret belongs to a different database or role")
            password = unquote(parsed.password or "")
            if len(password) < 32:
                raise ValueError("Existing database password does not satisfy the bootstrap policy")
        else:
            password = secrets.token_urlsafe(48)
        passwords[role] = password
        dsn = f"postgresql://{role}:{quote(password, safe='')}@{host}:5432/ledgerguard"
        if name == "application":
            app["DATABASE_URL"] = dsn
            payload = json.dumps(app)
        else:
            payload = dsn
        client.put_secret_value(SecretId=arns[name], SecretString=payload)
    certificate = Path(__file__).resolve().parents[1] / "infra/certificates/rds-eu-west-1.pem"
    with psycopg.connect(
        host=host,
        dbname="ledgerguard",
        user=master["username"],
        password=master["password"],
        sslmode="verify-full",
        sslrootcert=str(certificate),
        connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
            for role, password in passwords.items():
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
                verb = "ALTER" if cursor.fetchone() else "CREATE"
                cursor.execute(
                    sql.SQL(
                        verb + " ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
            cursor.execute("GRANT ledgerguard_migrator TO CURRENT_USER")
            cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cursor.execute(
                "GRANT CONNECT ON DATABASE ledgerguard TO ledgerguard_app, ledgerguard_migrator, ledgerguard_maintenance"
            )
            cursor.execute("GRANT USAGE, CREATE ON SCHEMA public TO ledgerguard_migrator")
            cursor.execute("GRANT USAGE ON SCHEMA public TO ledgerguard_app, ledgerguard_maintenance")
            cursor.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE ledgerguard_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ledgerguard_app, ledgerguard_maintenance"
            )
            cursor.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE ledgerguard_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ledgerguard_app, ledgerguard_maintenance"
            )
    print("Database roles and secret versions initialized. Run migrations before enabling services.")


if __name__ == "__main__":
    main()
