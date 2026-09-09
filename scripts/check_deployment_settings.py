"""Check production configuration without contacting external services."""

import json
import os
import subprocess
import sys

value = os.environ | {
    "DJANGO_SETTINGS_MODULE": "apps.control_plane.settings",
    "LEDGERGUARD_ENV": "production",
    "DJANGO_SECRET_KEY": "deployment-check-only-" + "x" * 64,
    "PUBLIC_URL": "https://checks.example.com",
    "ALLOWED_HOSTS": "checks.example.com",
    "DATABASE_URL": "postgresql://ledgerguard_app:deployment-check-password@database.checks.internal/ledgerguard",
    "DB_SSLMODE": "verify-full",
    "LEDGERGUARD_DATABASE_ROLE": "application",
    "AWS_REGION": "eu-west-1",
    "LOCAL_ENCRYPTION_KEY": "",
    "QUEUE_URL": "",
    "QUEUE_URLS": json.dumps(
        {
            name: f"https://sqs.eu-west-1.amazonaws.com/123456789012/checks-{name}"
            for name in ["ingestion", "reconciliation", "notification", "reporting", "repair"]
        }
    ),
    "WORKER_QUEUE_CLASS": "ingestion",
    "REPORT_BUCKET": "ledgerguard-checks-reports",
    "DELETION_LEDGER_BUCKET": "ledgerguard-checks-erasure-ledger",
    "SES_FROM_EMAIL": "alerts@checks.example.com",
    "SES_CONFIGURATION_SET": "ledgerguard-checks",
    "SES_FEEDBACK_QUEUE_URL": "https://sqs.eu-west-1.amazonaws.com/123456789012/checks-email-feedback",
    "COGNITO_ISSUER": "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_checks",
    "COGNITO_DOMAIN": "https://ledgerguard-checks.auth.eu-west-1.amazoncognito.com",
    "COGNITO_CLIENT_ID": "12345678checksclient",
    "COGNITO_CLIENT_SECRET": "deployment-check-client-secret",
    "COGNITO_MFA_ENFORCED": "1",
    "KMS_KEY_ID": "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-4000-8000-000000000000",
}
result = subprocess.run(
    [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"], env=value, check=False
)
raise SystemExit(result.returncode)
