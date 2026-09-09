"""Validate contract syntax and verify that every public API route is documented."""

import json
import os
import re
from pathlib import Path

import django
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.control_plane.settings")
    django.setup()
    from apps.control_plane.urls import urlpatterns

    root = Path(__file__).resolve().parents[1]
    contract = root / "contracts/openapi.json"
    document = json.loads(contract.read_text())
    validate(document, base_uri=contract.as_uri())
    for schema in (root / "contracts/schemas").glob("*.json"):
        Draft202012Validator.check_schema(json.loads(schema.read_text()))
    routes = {
        "/" + re.sub(r"<(?:uuid|str):(\w+)>", r"{\1}", str(route.pattern))
        for route in urlpatterns
        if str(route.pattern).startswith(("api/", "oauth/", "webhooks/"))
    }
    if routes != set(document["paths"]):
        raise SystemExit("Public routes and OpenAPI paths differ; rebuild and review the contract")
    print(f"Validated {len(routes)} public API paths and all versioned JSON schemas")


if __name__ == "__main__":
    main()
