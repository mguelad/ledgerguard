from typing import Any

from django.http import HttpRequest, JsonResponse
from rest_framework.views import exception_handler as drf_exception_handler


class Problem(Exception):
    def __init__(self, code: str, status: int = 400, detail: str = "The request could not be accepted.") -> None:
        self.code, self.status, self.detail = code, status, detail
        super().__init__(code)


def problem_response(request: HttpRequest, error: Problem) -> JsonResponse:
    return JsonResponse(
        {
            "type": f"urn:ledgerguard:problem:{error.code.lower().replace('_', '-')}",
            "title": error.code.replace("_", " ").capitalize(),
            "status": error.status,
            "code": error.code,
            "detail": error.detail,
            "correlation_id": getattr(request, "correlation_id", ""),
        },
        status=error.status,
        content_type="application/problem+json",
    )


def exception_handler(exc: Exception, context: dict[str, Any]) -> Any:
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = {
            "type": "urn:ledgerguard:problem:request-rejected",
            "title": "Request rejected",
            "status": response.status_code,
            "code": "REQUEST_REJECTED",
            "detail": "Check authentication, permissions and request fields.",
            "correlation_id": getattr(context.get("request"), "correlation_id", ""),
        }
        response["Content-Type"] = "application/problem+json"
    return response
