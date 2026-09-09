import os
from pathlib import Path
from urllib.parse import unquote

from apps.control_plane.configuration import RuntimeConfiguration

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIGURATION = RuntimeConfiguration.from_environ(os.environ)
ENVIRONMENT = CONFIGURATION.environment
PRODUCTION = CONFIGURATION.deployed
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1" and not PRODUCTION
SECRET_KEY = CONFIGURATION.secret_key
SECRET_KEY_FALLBACKS = list(CONFIGURATION.secret_key_fallbacks)
ALLOWED_HOSTS = list(CONFIGURATION.allowed_hosts)
PUBLIC_URL = CONFIGURATION.public_url
DATABASE_URL = CONFIGURATION.database_url
DB_SSLMODE = CONFIGURATION.db_sslmode
LEDGERGUARD_DATABASE_ROLE = CONFIGURATION.database_role
db = CONFIGURATION.database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db.path.lstrip("/"),
        "USER": unquote(db.username or ""),
        "PASSWORD": unquote(db.password or ""),
        "HOST": db.hostname,
        "PORT": db.port or 5432,
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "connect_timeout": 5,
            **({"sslrootcert": str(BASE_DIR / "infra/certificates/rds-eu-west-1.pem")} if PRODUCTION else {}),
            "sslmode": DB_SSLMODE,
        },
    }
}
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    *[f"modules.{m}" for m in ["accounts", "connectors", "ingestion", "findings", "reporting"]],
]
MIDDLEWARE = [
    "apps.control_plane.middleware.HealthMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.control_plane.middleware.SecurityContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "apps.control_plane.urls"
WSGI_APPLICATION = "apps.control_plane.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-gb"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
COLLECTED_STATIC = PRODUCTION or os.environ.get("STATIC_MANIFEST") == "1"
STATIC_ROOT = BASE_DIR / "staticfiles" if COLLECTED_STATIC else None
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if COLLECTED_STATIC
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
WHITENOISE_AUTOREFRESH = not COLLECTED_STATIC
WHITENOISE_USE_FINDERS = not COLLECTED_STATIC
SESSION_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SECURE = PRODUCTION
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 28800
SESSION_SAVE_EVERY_REQUEST = False
SESSION_IDLE_SECONDS = 1800
SECURE_SSL_REDIRECT = PRODUCTION
SECURE_REDIRECT_EXEMPT = [r"^health/(live|ready)$"]
SECURE_HSTS_SECONDS = 31536000 if PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PRODUCTION
SECURE_HSTS_PRELOAD = PRODUCTION
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if PRODUCTION else None
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
CSRF_TRUSTED_ORIGINS = [PUBLIC_URL] if PRODUCTION else []
DATA_UPLOAD_MAX_MEMORY_SIZE = 1048576
FILE_UPLOAD_MAX_MEMORY_SIZE = 0
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "apps.control_plane.errors.exception_handler",
}
AWS_REGION = CONFIGURATION.aws_region
KMS_KEY_ID = CONFIGURATION.kms_key_id
LOCAL_ENCRYPTION_KEY = CONFIGURATION.local_encryption_key
QUEUE_URL = CONFIGURATION.queue_url
REPORT_BUCKET = CONFIGURATION.report_bucket
DELETION_LEDGER_BUCKET = CONFIGURATION.deletion_ledger_bucket
SES_FROM_EMAIL = CONFIGURATION.ses_from_email
SES_CONFIGURATION_SET = CONFIGURATION.ses_configuration_set
EMAIL_BACKEND = (
    "django.core.mail.backends.console.EmailBackend"
    if not PRODUCTION
    else "django.core.mail.backends.smtp.EmailBackend"
)
STRIPE_DEVELOPER_TEST_KEY = os.environ.get("STRIPE_DEVELOPER_TEST_KEY", "")
STRIPE_DEVELOPER_LIVE_KEY = os.environ.get("STRIPE_DEVELOPER_LIVE_KEY", "")
STRIPE_TEST_CLIENT_ID = os.environ.get("STRIPE_TEST_CLIENT_ID", "")
STRIPE_LIVE_CLIENT_ID = os.environ.get("STRIPE_LIVE_CLIENT_ID", "")
STRIPE_API_VERSION = os.environ.get("STRIPE_API_VERSION", "2026-08-26.dahlia")
COGNITO_ISSUER = CONFIGURATION.cognito_issuer
COGNITO_DOMAIN = CONFIGURATION.cognito_domain
COGNITO_CLIENT_ID = CONFIGURATION.cognito_client_id
COGNITO_CLIENT_SECRET = CONFIGURATION.cognito_client_secret
COGNITO_MFA_ENFORCED = CONFIGURATION.cognito_mfa_enforced
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"safe": {"()": "apps.control_plane.telemetry.SafeFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "safe"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "httpx": {"level": "WARNING"},
        "botocore": {"level": "WARNING"},
    },
}

QUEUE_URLS = CONFIGURATION.queue_urls
WORKER_QUEUE_CLASS = CONFIGURATION.worker_queue_class
SES_FEEDBACK_QUEUE_URL = CONFIGURATION.ses_feedback_queue_url
