"""Dependency-upgrade regressions for the actual published API contracts."""

import json
import shutil
from pathlib import Path

import pytest
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from scripts.check_contracts import check_local_references, main

ROOT = Path(__file__).parents[1] / "contracts"


def test_published_contract_and_route_inventory(capsys):
    main()
    assert "Validated 26 public API paths" in capsys.readouterr().out


@pytest.mark.parametrize("invalid", ["missing_version", "invalid_schema", "unbound_parameter"])
def test_spec_validator_still_rejects_invalid_contracts(invalid):
    document = json.loads((ROOT / "openapi.json").read_text())
    if invalid == "missing_version":
        del document["info"]["version"]
    elif invalid == "invalid_schema":
        document["components"]["schemas"]["Problem"]["type"] = "not-a-valid-type"
    else:
        operation = document["paths"]["/api/v1/organizations/{organization_id}/stores"]["post"]
        operation["parameters"] = [parameter for parameter in operation["parameters"] if parameter["in"] != "path"]
    with pytest.raises(OpenAPIValidationError):
        validate(document, base_uri=(ROOT / "openapi.json").as_uri())


def test_route_inventory_mismatch_is_rejected(monkeypatch):
    from apps.control_plane import urls

    monkeypatch.setattr(urls, "urlpatterns", [])
    with pytest.raises(SystemExit, match="Public routes and OpenAPI paths differ"):
        main()


def test_command_rejects_missing_reference_inside_request_schema(tmp_path, monkeypatch):
    from scripts import check_contracts

    shutil.copytree(ROOT, tmp_path / "contracts")
    contract = tmp_path / "contracts/openapi.json"
    document = json.loads(contract.read_text())
    operation = document["paths"]["/api/v1/plugin/facts/batch"]["post"]
    operation["requestBody"]["content"]["application/json"]["schema"]["$ref"] = "./schemas/missing.json"
    contract.write_text(json.dumps(document))
    monkeypatch.setattr(check_contracts, "__file__", str(tmp_path / "scripts/check_contracts.py"))
    with pytest.raises(ValueError, match="Invalid local contract reference"):
        main()


@pytest.mark.parametrize(
    "reference",
    [
        "./missing.json",
        "#/missing",
        "./schema.json#/missing",
        "./schema.json#/items/2",
        "./schema.json#/items/01",
        "./schema.json#/items/0/type/missing",
        "./schema.json#/a~2b",
        "./schema.json#unsupported-anchor",
        "https://example.invalid/schema.json",
        "//example.invalid/schema.json",
        "./schema.json?external=1",
        "../outside.json",
        123,
    ],
)
def test_unresolvable_or_nonlocal_schema_references_fail_offline(tmp_path, reference):
    (tmp_path / "openapi.json").write_text(json.dumps({"nested": [{"$ref": reference}]}))
    (tmp_path / "schema.json").write_text(json.dumps({"items": [{"type": "string"}]}))
    with pytest.raises(ValueError, match="Invalid local contract reference"):
        check_local_references(tmp_path)


def test_local_references_resolve_pointers_and_cycles_without_recursing(tmp_path):
    (tmp_path / "openapi.json").write_text(json.dumps({"$ref": "./schema.json#/a~1b/~0/items/0"}))
    (tmp_path / "schema.json").write_text(
        json.dumps({"a/b": {"~": {"items": [{"$ref": "./openapi.json"}]}}, "space key": {"$ref": "#"}})
    )
    (tmp_path / "encoded.json").write_text(json.dumps({"$ref": "./schema.json#/space%20key"}))
    check_local_references(tmp_path)


def test_schema_reference_cannot_escape_via_symlink(tmp_path):
    root = tmp_path / "contracts"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (root / "escape.json").symlink_to(outside)
    (root / "openapi.json").write_text(json.dumps({"$ref": "./escape.json"}))
    with pytest.raises(ValueError, match="Contract document escapes root"):
        check_local_references(root)
