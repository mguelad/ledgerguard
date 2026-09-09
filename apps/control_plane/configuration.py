"""Typed, fail-closed loading for process-level runtime configuration."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import ParseResult, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

CLOUD_ENVIRONMENTS = frozenset({"dev", "staging", "production"})
ENVIRONMENTS = CLOUD_ENVIRONMENTS | {"development"}
QUEUE_CLASSES = frozenset({"ingestion", "reconciliation", "notification", "reporting", "repair"})
DATABASE_USERS = {
    "application": "ledgerguard_app",
    "migrator": "ledgerguard_migrator",
    "maintenance": "ledgerguard_maintenance",
}
S3_BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
SQS_QUEUE_PATH_PATTERN = re.compile(r"/(?P<account>[0-9]{12})/[A-Za-z0-9_-]{1,80}")


def _postgresql_url(value: str) -> ParseResult:
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise ImproperlyConfigured("DATABASE_URL is invalid") from exc
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not parsed.username
        or not parsed.path.lstrip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ImproperlyConfigured("DATABASE_URL must identify one PostgreSQL database")
    return parsed


def _https_url(name: str, value: str, *, allow_path: bool = False) -> ParseResult:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path not in {"", "/"})
    ):
        suffix = "HTTPS URL" if allow_path else "exact HTTPS origin"
        raise ImproperlyConfigured(f"{name} must be an {suffix}")
    return parsed


def _queue_urls(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured("QUEUE_URLS must be a JSON object") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ImproperlyConfigured("QUEUE_URLS must map queue classes to URLs")
    return value


def _valid_s3_bucket(value: str) -> bool:
    return bool(
        S3_BUCKET_PATTERN.fullmatch(value)
        and ".." not in value
        and ".-" not in value
        and "-." not in value
        and not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", value)
    )


@dataclass(frozen=True)
class RuntimeConfiguration:
    environment: str
    secret_key: str
    secret_key_fallbacks: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    public_url: str
    database_url: str
    database_supplied: bool
    db_sslmode: str
    database_role: str
    aws_region: str
    kms_key_id: str
    local_encryption_key: str
    queue_url: str
    queue_urls: dict[str, str]
    worker_queue_class: str
    report_bucket: str
    deletion_ledger_bucket: str
    ses_from_email: str
    ses_configuration_set: str
    ses_feedback_queue_url: str
    cognito_issuer: str
    cognito_domain: str
    cognito_client_id: str
    cognito_client_secret: str
    cognito_mfa_enforced: bool

    @property
    def deployed(self) -> bool:
        return self.environment in CLOUD_ENVIRONMENTS

    @property
    def database(self) -> ParseResult:
        return _postgresql_url(self.database_url)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "RuntimeConfiguration":
        environment = environ.get("LEDGERGUARD_ENV", "development")
        if environment not in ENVIRONMENTS:
            raise ImproperlyConfigured("LEDGERGUARD_ENV must be development, dev, staging, or production")
        database_default = "postgresql://ledgerguard_app:local-development@localhost:5432/ledgerguard"
        value = cls(
            environment=environment,
            secret_key=environ.get("DJANGO_SECRET_KEY", "development-only-session-key-change-before-deploy"),
            secret_key_fallbacks=tuple(filter(None, environ.get("DJANGO_SECRET_KEY_FALLBACKS", "").split(","))),
            allowed_hosts=tuple(
                filter(
                    None,
                    (
                        host.strip()
                        for host in environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
                    ),
                )
            ),
            public_url=environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            database_url=environ.get("DATABASE_URL", database_default),
            database_supplied=bool(environ.get("DATABASE_URL", "").strip()),
            db_sslmode=environ.get("DB_SSLMODE", "verify-full" if environment in CLOUD_ENVIRONMENTS else "prefer"),
            database_role=environ.get("LEDGERGUARD_DATABASE_ROLE", ""),
            aws_region=environ.get("AWS_REGION", "eu-west-1"),
            kms_key_id=environ.get("KMS_KEY_ID", ""),
            local_encryption_key=environ.get("LOCAL_ENCRYPTION_KEY", ""),
            queue_url=environ.get("QUEUE_URL", ""),
            queue_urls=_queue_urls(environ.get("QUEUE_URLS", "{}")),
            worker_queue_class=environ.get("WORKER_QUEUE_CLASS", "ingestion"),
            report_bucket=environ.get("REPORT_BUCKET", ""),
            deletion_ledger_bucket=environ.get("DELETION_LEDGER_BUCKET", ""),
            ses_from_email=environ.get("SES_FROM_EMAIL", ""),
            ses_configuration_set=environ.get("SES_CONFIGURATION_SET", ""),
            ses_feedback_queue_url=environ.get("SES_FEEDBACK_QUEUE_URL", ""),
            cognito_issuer=environ.get("COGNITO_ISSUER", ""),
            cognito_domain=environ.get("COGNITO_DOMAIN", ""),
            cognito_client_id=environ.get("COGNITO_CLIENT_ID", ""),
            cognito_client_secret=environ.get("COGNITO_CLIENT_SECRET", ""),
            cognito_mfa_enforced=environ.get("COGNITO_MFA_ENFORCED", "0") == "1",
        )
        value.validate()
        return value

    def validate(self) -> None:
        database = self.database
        if self.worker_queue_class not in QUEUE_CLASSES:
            raise ImproperlyConfigured("WORKER_QUEUE_CLASS is invalid")
        if self.database_role and self.database_role not in DATABASE_USERS:
            raise ImproperlyConfigured("LEDGERGUARD_DATABASE_ROLE is invalid")
        if not self.deployed:
            return

        if self.secret_key.startswith("development-") or len(self.secret_key) < 40:
            raise ImproperlyConfigured("DJANGO_SECRET_KEY is required")
        if any(key.startswith("development-") or len(key) < 40 for key in self.secret_key_fallbacks):
            raise ImproperlyConfigured("DJANGO_SECRET_KEY_FALLBACKS contains an unsafe key")
        public = _https_url("PUBLIC_URL", self.public_url)
        if self.allowed_hosts != (public.hostname,):
            raise ImproperlyConfigured("ALLOWED_HOSTS must contain only the PUBLIC_URL hostname")
        if not self.database_supplied:
            raise ImproperlyConfigured("DATABASE_URL is required")
        if self.db_sslmode != "verify-full":
            raise ImproperlyConfigured("DB_SSLMODE must be verify-full")
        if self.database_role not in DATABASE_USERS:
            raise ImproperlyConfigured("LEDGERGUARD_DATABASE_ROLE is required")
        if not unquote(database.password or ""):
            raise ImproperlyConfigured("DATABASE_URL must include a database password")
        if unquote(database.username or "") != DATABASE_USERS[self.database_role]:
            raise ImproperlyConfigured("DATABASE_URL user does not match LEDGERGUARD_DATABASE_ROLE")
        if self.aws_region != "eu-west-1":
            raise ImproperlyConfigured("AWS_REGION must be eu-west-1")
        if self.local_encryption_key:
            raise ImproperlyConfigured("LOCAL_ENCRYPTION_KEY is forbidden outside local development")
        kms = re.fullmatch(r"arn:aws:kms:eu-west-1:(?P<account>[0-9]{12}):key/[A-Za-z0-9-]+", self.kms_key_id)
        if not kms:
            raise ImproperlyConfigured("KMS_KEY_ID must be an eu-west-1 key ARN")

        required = {
            "REPORT_BUCKET": self.report_bucket,
            "DELETION_LEDGER_BUCKET": self.deletion_ledger_bucket,
            "SES_FROM_EMAIL": self.ses_from_email,
            "SES_CONFIGURATION_SET": self.ses_configuration_set,
            "SES_FEEDBACK_QUEUE_URL": self.ses_feedback_queue_url,
            "COGNITO_ISSUER": self.cognito_issuer,
            "COGNITO_DOMAIN": self.cognito_domain,
            "COGNITO_CLIENT_ID": self.cognito_client_id,
            "COGNITO_CLIENT_SECRET": self.cognito_client_secret,
        }
        missing = sorted(name for name, item in required.items() if not item)
        if missing:
            raise ImproperlyConfigured("Required deployment configuration is missing: " + ", ".join(missing))
        if self.report_bucket == self.deletion_ledger_bucket:
            raise ImproperlyConfigured("Report and erasure-ledger buckets must be separate")
        if not _valid_s3_bucket(self.report_bucket) or not _valid_s3_bucket(self.deletion_ledger_bucket):
            raise ImproperlyConfigured("S3 bucket names are invalid")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", self.ses_from_email):
            raise ImproperlyConfigured("SES_FROM_EMAIL is invalid")
        if not self.cognito_mfa_enforced:
            raise ImproperlyConfigured("Cognito MFA must be required")

        issuer = _https_url("COGNITO_ISSUER", self.cognito_issuer, allow_path=True)
        if issuer.hostname != "cognito-idp.eu-west-1.amazonaws.com" or not re.fullmatch(
            r"/eu-west-1_[A-Za-z0-9]+", issuer.path
        ):
            raise ImproperlyConfigured("COGNITO_ISSUER must identify an eu-west-1 user pool")
        domain = _https_url("COGNITO_DOMAIN", self.cognito_domain)
        if not domain.hostname or not domain.hostname.endswith(".auth.eu-west-1.amazoncognito.com"):
            raise ImproperlyConfigured("COGNITO_DOMAIN must identify an eu-west-1 hosted UI")
        if not re.fullmatch(r"[A-Za-z0-9]{8,128}", self.cognito_client_id) or len(self.cognito_client_secret) < 16:
            raise ImproperlyConfigured("Cognito client credentials are invalid")

        if self.queue_url:
            raise ImproperlyConfigured("QUEUE_URL is not allowed when deployed; configure every queue class")
        if set(self.queue_urls) != QUEUE_CLASSES:
            raise ImproperlyConfigured("QUEUE_URLS must configure exactly the five queue classes")
        all_queue_urls = [*self.queue_urls.values(), self.ses_feedback_queue_url]
        if len(set(all_queue_urls)) != len(all_queue_urls):
            raise ImproperlyConfigured("Every SQS queue URL must be distinct")
        for queue_url in all_queue_urls:
            queue = _https_url("SQS queue URL", queue_url, allow_path=True)
            queue_path = SQS_QUEUE_PATH_PATTERN.fullmatch(queue.path)
            if (
                queue.hostname != "sqs.eu-west-1.amazonaws.com"
                or not queue_path
                or queue_path.group("account") != kms.group("account")
            ):
                raise ImproperlyConfigured("SQS queue URLs must identify eu-west-1 queues")
