"""Validate contract syntax and verify that every public API route is documented."""

import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import django
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate


def check_local_references(root: Path) -> None:
    """Resolve every contract $ref offline, including refs inside Schema Objects.

    Spec validation alone does not visit every JSON Schema reference. Our
    published contracts use local JSON files and JSON Pointer fragments only;
    remote references would make their validation depend on external services.
    """
    root = root.resolve()
    documents = {}
    for path in root.rglob("*.json"):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"Contract document escapes root: {path.relative_to(root)}")
        documents[resolved] = json.loads(path.read_text())
    for source, document in documents.items():
        pending = [document]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                pending.extend(node.values())
                if "$ref" not in node:
                    continue
                reference = node["$ref"]
                try:
                    if not isinstance(reference, str):
                        raise ValueError("$ref must be a string")
                    parts = urlsplit(reference)
                    if parts.scheme or parts.netloc or parts.query:
                        raise ValueError("only local references are supported")
                    target = (source.parent / unquote(parts.path, errors="strict")).resolve() if parts.path else source
                    target.relative_to(root)
                    value = documents[target]
                    fragment = unquote(parts.fragment, errors="strict")
                    if fragment and not fragment.startswith("/"):
                        raise ValueError("fragment must be a JSON Pointer")
                    for token in fragment.split("/")[1:]:
                        if re.search(r"~(?:[^01]|$)", token):
                            raise ValueError("invalid JSON Pointer escape")
                        token = token.replace("~1", "/").replace("~0", "~")
                        if isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
                            value = value[int(token)]
                        elif isinstance(value, dict):
                            value = value[token]
                        else:
                            raise ValueError("JSON Pointer does not resolve")
                except (ValueError, KeyError, IndexError) as exc:
                    raise ValueError(
                        f"Invalid local contract reference in {source.relative_to(root)}: {reference!r}"
                    ) from exc


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.control_plane.settings")
    django.setup()
    from apps.control_plane.urls import urlpatterns

    root = Path(__file__).resolve().parents[1]
    contract = root / "contracts/openapi.json"
    document = json.loads(contract.read_text())
    check_local_references(root / "contracts")
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
