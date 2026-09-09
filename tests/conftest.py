"""Integration tests use a disposable PostgreSQL database and the real runtime role."""

import os
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.backends.signals import connection_created


def pytest_collection_modifyitems(items):
    if os.environ.get("LEDGERGUARD_INTEGRATION") != "1":
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason="Set LEDGERGUARD_INTEGRATION=1 with a disposable PostgreSQL database")
                )


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker):
    if os.environ.get("LEDGERGUARD_INTEGRATION") != "1":
        return
    # This fixture never creates or drops a database. CI provisions an isolated database explicitly.
    if not connection.settings_dict["NAME"].endswith("_test") and os.environ.get("LEDGERGUARD_PGLITE") != "1":
        raise RuntimeError("Integration database name must end in _test")
    with django_db_blocker.unblock():
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("SELECT rolname FROM pg_roles WHERE rolname='ledgerguard_app'")
            if not cursor.fetchone():
                sql = (Path(__file__).parents[1] / "infra/local/bootstrap.sql").read_text()
                # Database identifier comes from the administrator's test settings, never a request.
                sql = sql.replace(
                    "DATABASE ledgerguard", "DATABASE " + connection.ops.quote_name(connection.settings_dict["NAME"])
                )
                cursor.execute(sql)
            cursor.execute("SET ROLE ledgerguard_migrator")
        call_command("migrate", interactive=False, verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE ledgerguard_app")
            cursor.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            assert cursor.fetchone() == ("ledgerguard_app", False, False)

    def runtime_role(sender, connection, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE ledgerguard_app")

    connection_created.connect(runtime_role, weak=False, dispatch_uid="test_runtime_role")
    yield
    connection_created.disconnect(dispatch_uid="test_runtime_role")


@pytest.fixture
def local_crypto(settings):
    import base64

    settings.KMS_KEY_ID = ""
    settings.LOCAL_ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
    settings.PRODUCTION = False
