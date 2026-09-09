import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.utils import timezone

from apps.control_plane.errors import Problem, problem_response
from modules.accounts.models import AuditLog, Membership, Organization, SupportGrant
from modules.connectors.models import ResourceLocator

SECURITY_NOTIFICATION_ACTIONS = frozenset(
    {
        "alert_route.updated",
        "connector.disconnected",
        "connector.suspended",
        "member.changed",
        "member.invited",
        "member.joined",
        "organization.deletion_requested",
        "report.requested",
        "stripe.connected",
        "stripe.webhook_secret_updated",
        "support.approved",
        "support.revoked",
        "woo.key_rotated",
        "woo.paired",
    }
)


def require_database_role(expected: str) -> None:
    """Refuse privileged or misrouted database sessions at process boundaries."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        identity = cursor.fetchone()
    if identity != (expected, False, False):
        raise PermissionError(f"{expected} database role required")


@contextmanager
def tenant_scope(organization_id: UUID | str, user_id: int | None = None) -> Iterator[None]:
    org = str(UUID(str(organization_id)))
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.organization_id',true), current_setting('app.user_id',true)")
            old_org, old_user = cursor.fetchone()
            if old_org and old_org != org:
                raise Problem("TENANT_CONTEXT_CONFLICT", 403)
            cursor.execute(
                "SELECT set_config('app.organization_id',%s,true),set_config('app.user_id',%s,true)",
                [org, str(user_id or "")],
            )
        completed = False
        try:
            yield
            completed = True
        finally:
            if completed and not connection.needs_rollback:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('app.organization_id',%s,true),set_config('app.user_id',%s,true)",
                        [old_org or "", old_user or ""],
                    )


def audit(
    org: UUID | str,
    actor: str,
    action: str,
    resource: UUID | str,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
) -> AuditLog:
    organization_id = UUID(str(org))
    row = AuditLog.objects.create(
        organization_id=organization_id,
        actor_id=actor,
        action=action,
        resource_id=str(resource),
        details=details or {},
        correlation_id=correlation_id,
    )
    if action in SECURITY_NOTIFICATION_ACTIONS:
        from modules.ingestion.service import enqueue

        enqueue(organization_id, "security_notify", row.id, f"security-notify:{row.id}")
    return row


def require_recent_auth(request: HttpRequest) -> None:
    if time.time() - float(request.session.get("authenticated_at", 0)) > 600:
        raise Problem("RECENT_AUTHENTICATION_REQUIRED", 403, "Sign in again before this operation.")


def require_role(request: HttpRequest, *roles: str) -> None:
    if getattr(request, "tenant_role", None) not in roles:
        raise Problem("FORBIDDEN", 403)


def tenant_view(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            if not request.user.is_authenticated:
                raise Problem("AUTHENTICATION_REQUIRED", 401)
            org = kwargs.get("organization_id")
            resource_id = next(
                (kwargs[k] for k in ("store_id", "finding_id", "report_id", "connector_id") if k in kwargs), None
            )
            if resource_id is not None:
                locator = ResourceLocator.objects.filter(id=resource_id).first()
                if locator is None:
                    raise Problem("NOT_FOUND", 404)
                org = locator.organization_id
            org = org or request.session.get("organization_id")
            if not org:
                raise Problem("ORGANIZATION_REQUIRED", 400)
            with tenant_scope(org, request.user.pk):
                membership = Membership.objects.filter(organization_id=org, user=request.user, active=True).first()
                grant = None
                if membership is None and request.user.is_staff:
                    grant = SupportGrant.objects.filter(
                        organization_id=org,
                        support_user=request.user,
                        revoked_at__isnull=True,
                        expires_at__gt=timezone.now(),
                    ).first()
                if membership is None and grant is None:
                    raise Problem("NOT_FOUND", 404)
                if not Organization.objects.filter(id=org, active=True).exists():
                    raise Problem("ORGANIZATION_INACTIVE", 403)
                if membership and membership.role == "merchant":
                    store_id = kwargs.get("store_id")
                    if kwargs.get("finding_id"):
                        from modules.findings.models import Finding

                        store_id = (
                            Finding.objects.filter(id=kwargs["finding_id"]).values_list("store_id", flat=True).first()
                        )
                    if not store_id or str(store_id) != str(membership.store_id):
                        raise Problem("NOT_FOUND", 404)
                request.organization_id = UUID(str(org))  # type: ignore[attr-defined]
                request.tenant_role = membership.role if membership else "support"  # type: ignore[attr-defined]
                request.support_access = grant is not None  # type: ignore[attr-defined]
                request.session["organization_id"] = str(org)
                if grant:
                    audit(org, str(request.user.pk), "support.request", resource_id or org, {"method": request.method})
                response = view(request, *args, **kwargs)
                if getattr(response, "streaming", False):
                    raise RuntimeError("Tenant views must materialize responses inside the transaction")
                return response
        except Problem as error:
            return problem_response(request, error)
        except Http404:
            return problem_response(request, Problem("NOT_FOUND", 404))
        except ValidationError:
            return problem_response(request, Problem("VALIDATION_FAILED", 422))

    return wrapped
