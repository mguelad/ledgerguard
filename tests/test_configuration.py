import json

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.control_plane.configuration import RuntimeConfiguration


def deployed_environment() -> dict[str, str]:
    queues = {
        name: f"https://sqs.eu-west-1.amazonaws.com/123456789012/ledgerguard-{name}"
        for name in ["ingestion", "reconciliation", "notification", "reporting", "repair"]
    }
    return {
        "LEDGERGUARD_ENV": "production",
        "DJANGO_SECRET_KEY": "production-session-key-" + "x" * 48,
        "PUBLIC_URL": "https://ledgerguard.example",
        "ALLOWED_HOSTS": "ledgerguard.example",
        "DATABASE_URL": "postgresql://ledgerguard_app:password@database.internal/ledgerguard",
        "DB_SSLMODE": "verify-full",
        "LEDGERGUARD_DATABASE_ROLE": "application",
        "AWS_REGION": "eu-west-1",
        "KMS_KEY_ID": "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-4000-8000-000000000000",
        "QUEUE_URLS": json.dumps(queues),
        "WORKER_QUEUE_CLASS": "ingestion",
        "REPORT_BUCKET": "ledgerguard-production-reports",
        "DELETION_LEDGER_BUCKET": "ledgerguard-production-erasure",
        "SES_FROM_EMAIL": "alerts@ledgerguard.example",
        "SES_CONFIGURATION_SET": "ledgerguard-production",
        "SES_FEEDBACK_QUEUE_URL": ("https://sqs.eu-west-1.amazonaws.com/123456789012/ledgerguard-email-feedback"),
        "COGNITO_ISSUER": "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_fixture",
        "COGNITO_DOMAIN": "https://ledgerguard.auth.eu-west-1.amazoncognito.com",
        "COGNITO_CLIENT_ID": "12345678fixtureclient",
        "COGNITO_CLIENT_SECRET": "fixture-client-secret-value",
        "COGNITO_MFA_ENFORCED": "1",
    }


def test_deployed_configuration_is_complete_and_role_bound():
    configuration = RuntimeConfiguration.from_environ(deployed_environment())
    assert configuration.deployed
    assert configuration.database.username == "ledgerguard_app"
    assert set(configuration.queue_urls) == {
        "ingestion",
        "reconciliation",
        "notification",
        "reporting",
        "repair",
    }


@pytest.mark.parametrize(
    ("role", "username"),
    [
        ("application", "ledgerguard_app"),
        ("migrator", "ledgerguard_migrator"),
        ("maintenance", "ledgerguard_maintenance"),
    ],
)
def test_each_deployed_database_role_accepts_only_its_bound_login(role, username):
    environment = deployed_environment() | {
        "LEDGERGUARD_DATABASE_ROLE": role,
        "DATABASE_URL": f"postgresql://{username}:password@database.internal/ledgerguard",
    }
    assert RuntimeConfiguration.from_environ(environment).database_role == role


@pytest.mark.parametrize(
    ("changes", "removed", "message"),
    [
        ({"LEDGERGUARD_ENV": "prodution"}, (), "LEDGERGUARD_ENV"),
        ({"DJANGO_SECRET_KEY": "short"}, (), "DJANGO_SECRET_KEY"),
        ({"DJANGO_SECRET_KEY_FALLBACKS": "old-short-key"}, (), "FALLBACKS"),
        ({"PUBLIC_URL": "http://ledgerguard.example"}, (), "PUBLIC_URL"),
        ({"PUBLIC_URL": "https://ledgerguard.example/application"}, (), "PUBLIC_URL"),
        ({"ALLOWED_HOSTS": "ledgerguard.example,internal.example"}, (), "ALLOWED_HOSTS"),
        ({}, ("DATABASE_URL",), "DATABASE_URL is required"),
        ({"DATABASE_URL": "postgresql://ledgerguard_app@database.internal/ledgerguard"}, (), "password"),
        ({"DB_SSLMODE": "require"}, (), "verify-full"),
        ({}, ("LEDGERGUARD_DATABASE_ROLE",), "DATABASE_ROLE is required"),
        ({"LEDGERGUARD_DATABASE_ROLE": "maintenance"}, (), "does not match"),
        ({"AWS_REGION": "us-east-1"}, (), "eu-west-1"),
        ({"LOCAL_ENCRYPTION_KEY": "local-only-key"}, (), "forbidden"),
        ({"KMS_KEY_ID": "alias/ledgerguard"}, (), "key ARN"),
        ({}, ("COGNITO_CLIENT_SECRET",), "COGNITO_CLIENT_SECRET"),
        ({"COGNITO_MFA_ENFORCED": "0"}, (), "MFA"),
        ({"COGNITO_ISSUER": "https://identity.example/pool"}, (), "COGNITO_ISSUER"),
        ({"COGNITO_DOMAIN": "https://identity.example"}, (), "COGNITO_DOMAIN"),
        ({"QUEUE_URL": "https://sqs.eu-west-1.amazonaws.com/123456789012/fallback"}, (), "QUEUE_URL"),
        ({"QUEUE_URLS": "{}"}, (), "five queue classes"),
        (
            {
                "QUEUE_URLS": json.dumps(
                    {
                        name: "https://sqs.eu-west-1.amazonaws.com/123456789012/shared"
                        for name in ["ingestion", "reconciliation", "notification", "reporting", "repair"]
                    }
                )
            },
            (),
            "must be distinct",
        ),
        (
            {"SES_FEEDBACK_QUEUE_URL": "https://sqs.eu-west-1.amazonaws.com/999999999999/email-feedback"},
            (),
            "eu-west-1 queues",
        ),
        ({"SES_FROM_EMAIL": "not-an-address"}, (), "SES_FROM_EMAIL"),
        ({"DELETION_LEDGER_BUCKET": "ledgerguard-production-reports"}, (), "must be separate"),
        ({"DELETION_LEDGER_BUCKET": "ledgerguard..erasure"}, (), "bucket names"),
    ],
)
def test_deployed_configuration_rejects_unsafe_values(changes, removed, message):
    environment = deployed_environment() | changes
    for name in removed:
        environment.pop(name)
    with pytest.raises(ImproperlyConfigured, match=message):
        RuntimeConfiguration.from_environ(environment)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"QUEUE_URLS": "not-json"}, "JSON object"),
        ({"QUEUE_URLS": "[]"}, "map queue classes"),
        ({"WORKER_QUEUE_CLASS": "unknown"}, "WORKER_QUEUE_CLASS"),
        ({"DATABASE_URL": "sqlite:///ledgerguard"}, "PostgreSQL"),
    ],
)
def test_local_configuration_rejects_malformed_process_contract(changes, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        RuntimeConfiguration.from_environ(changes)
