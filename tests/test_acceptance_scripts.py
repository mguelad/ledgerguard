import json
import sys
from unittest.mock import Mock

import pytest

from scripts import build_stripe_app, check_aws_readiness


def test_manifest_cli_builds_and_checks_same_contract(tmp_path, monkeypatch):
    output = tmp_path / "stripe-app.json"
    args = ["build", "--app-id", "com.ledgerguard.ci", "--origin", "https://staging.example.com"]
    monkeypatch.setattr(sys, "argv", args + ["--output", str(output)])
    build_stripe_app.main()
    assert json.loads(output.read_text())["name"] == "LedgerGuard"
    monkeypatch.setattr(sys, "argv", args + ["--check", str(output)])
    build_stripe_app.main()
    output.write_text("not-json")
    with pytest.raises(SystemExit, match="rejected"):
        build_stripe_app.main()


@pytest.mark.parametrize("passed", [False, True])
def test_aws_cli_writes_scoped_report_and_propagates_failure(tmp_path, monkeypatch, passed):
    config = tmp_path / "config.json"
    config.write_text("{}")
    output = tmp_path / "result.json"
    report = {
        "passed": passed,
        "production_accepted": False,
        "checks": [{"status": "pass" if passed else "error", "name": "test"}],
    }
    monkeypatch.setattr(check_aws_readiness, "inspect", Mock(return_value=report))
    monkeypatch.setattr(check_aws_readiness.boto3, "Session", Mock())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check",
            "--config",
            str(config),
            "--account-id",
            "123456789012",
            "--environment",
            "staging",
            "--image",
            "fixture",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        check_aws_readiness.main()
    assert exc.value.code == (0 if passed else 1)
    assert json.loads(output.read_text()) == report


def test_aws_cli_rejects_malformed_config_without_a_success_report(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("not-json")
    inspect = Mock()
    monkeypatch.setattr(check_aws_readiness, "inspect", inspect)
    monkeypatch.setattr(check_aws_readiness.boto3, "Session", Mock())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check",
            "--config",
            str(config),
            "--account-id",
            "123456789012",
            "--environment",
            "staging",
            "--image",
            "fixture",
        ],
    )
    with pytest.raises(SystemExit, match="Invalid acceptance configuration"):
        check_aws_readiness.main()
    inspect.assert_not_called()
