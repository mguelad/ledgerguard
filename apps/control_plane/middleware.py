import secrets
import time
from collections.abc import Callable

from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse

from apps.control_plane.telemetry import metric, new_trace, trace_context


class HealthMiddleware:
    """ALB checks use a private target IP as Host and carry no application session."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method in {"GET", "HEAD"} and request.path in {"/health/live", "/health/ready"}:
            from apps.control_plane.health import live, ready

            response = ready(request) if request.path.endswith("ready") else live(request)
            response["Cache-Control"] = "no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        return self.get_response(request)


class SecurityContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.correlation_id = secrets.token_hex(16)  # type: ignore[attr-defined]
        now = time.time()
        if request.user.is_authenticated:
            authenticated = float(request.session.get("authenticated_at", 0))
            last_active = float(request.session.get("last_active_at", authenticated))
            if authenticated and (
                now - authenticated > settings.SESSION_COOKIE_AGE or now - last_active > settings.SESSION_IDLE_SECONDS
            ):
                logout(request)
            elif authenticated:
                request.session["last_active_at"] = now
        trace = new_trace(request.headers.get("traceparent", ""))
        token = trace_context.set(trace)
        started = time.monotonic()
        try:
            response = self.get_response(request)
            metric(
                "WebhookDurationMs" if request.path.startswith("/webhooks/") else "RequestDurationMs",
                (time.monotonic() - started) * 1000,
            )
        finally:
            trace_context.reset(token)
        response["traceparent"] = trace
        response["X-Correlation-ID"] = request.correlation_id  # type: ignore[attr-defined]
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        response["X-Frame-Options"] = "DENY"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.user.is_authenticated or request.path.startswith(("/oauth/", "/api/", "/webhooks/")):
            response["Cache-Control"] = "no-store"
        return response
