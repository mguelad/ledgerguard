import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from jsonschema import Draft202012Validator, FormatChecker

from apps.control_plane.errors import Problem

MAX_BODY = 1_048_576


def timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC timestamp required")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("Duplicate JSON member")
        output[key] = value
    return output


def parse_body(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_BODY:
        raise Problem("PAYLOAD_TOO_LARGE", 413)
    depth, quoted, escaped = 0, False, False
    for char in body:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (123, 91):
            depth += 1
            if depth > 12:
                raise Problem("JSON_DEPTH_EXCEEDED", 400)
        elif char in (125, 93):
            depth -= 1
    try:
        value = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite number")),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise Problem("INVALID_JSON", 400) from None
    if not isinstance(value, dict):
        raise Problem("INVALID_JSON", 400)
    return value


def request_body(request: HttpRequest) -> bytes:
    if request.headers.get("Content-Encoding", "identity") != "identity":
        raise Problem("UNSUPPORTED_ENCODING", 415)
    if request.content_type != "application/json":
        raise Problem("UNSUPPORTED_MEDIA_TYPE", 415)
    try:
        length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        raise Problem("INVALID_CONTENT_LENGTH", 400) from None
    if length > MAX_BODY or length < 0:
        raise Problem("PAYLOAD_TOO_LARGE", 413)
    body = request.body
    if len(body) > MAX_BODY:
        raise Problem("PAYLOAD_TOO_LARGE", 413)
    return body


@lru_cache(maxsize=16)
def validator(name: str) -> Draft202012Validator:
    path = settings.BASE_DIR / "contracts" / "schemas" / name
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate(value: dict[str, Any], schema: str) -> None:
    if next(validator(schema).iter_errors(value), None) is not None:
        raise Problem("SCHEMA_VALIDATION_FAILED", 422, "The envelope contains unsupported or invalid fields.")


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
