from django.db import connection
from django.http import HttpRequest, JsonResponse

from modules.accounts.tenancy import require_database_role


def live(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def ready(request: HttpRequest) -> JsonResponse:
    try:
        require_database_role("ledgerguard_app")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND tableowner=current_user AND tablename LIKE 'findings_%')"
            )
            owner = cursor.fetchone()[0]
            cursor.execute(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid=to_regclass('public.findings_finding')"
            )
            secured = cursor.fetchone()
            cursor.execute("SELECT to_regclass('public.django_session') IS NOT NULL")
            sessions = cursor.fetchone()[0]
        if owner or secured != (True,) or not sessions:
            return JsonResponse({"status": "unavailable"}, status=503)
        return JsonResponse({"status": "ready"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
