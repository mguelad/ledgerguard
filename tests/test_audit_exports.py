import csv
import io
import json
import sys
from pathlib import Path

import pytest

from modules.reporting import cli
from modules.reporting.exports import csv_report, html_report, pdf_report


def test_offline_audit_creates_all_formats_without_copying_inputs(tmp_path, monkeypatch):
    fixture = Path(__file__).parents[1] / "fixtures/csv"
    paths = [fixture / filename for filename in ["stripe.csv", "woo.csv", "refunds.csv"]]
    before = [p.read_bytes() for p in paths]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ledgerguard-audit",
            "--stripe-csv",
            str(paths[0]),
            "--woo-csv",
            str(paths[1]),
            "--refund-csv",
            str(paths[2]),
            "--organization",
            "northwind",
            "--store",
            "store_one",
            "--account",
            "acct_one",
            "--mode",
            "test",
            "--as-of",
            "2026-09-05T12:00:00Z",
            "--covered-from",
            "2026-08-06T12:00:00Z",
            "--covered-through",
            "2026-09-05T11:59:00Z",
            "--output",
            str(tmp_path),
        ],
    )
    cli.main()
    assert {p.name for p in tmp_path.iterdir()} == {"findings.csv", "report.html", "report.pdf", "evidence.json"}
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert {p["rule"] for p in evidence["proposals"]} == {"PI-001"}
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF-")
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize("body", ["id,id\n1,1\n", "wrong\n1\n", "id,amount\n1\n", "id\n1,2\n"])
def test_malformed_csv_is_rejected_before_evaluation(tmp_path, body):
    path = tmp_path / "bad.csv"
    path.write_text(body)
    with pytest.raises(ValueError):
        cli.rows(path, {"id", "amount"})


def test_reports_escape_untrusted_text_and_preserve_integer_amounts():
    row = {
        "rule": "PI-001",
        "severity": "critical",
        "state": "open",
        "store": '=HYPERLINK("https://example.com")',
        "identities": "<script>alert(1)</script>",
        "amount_minor": 12995,
        "currency": "EUR",
        "exponent": 2,
        "confidence": 100,
        "mode": "test",
        "first_seen": "2026-09-05T12:00:00Z",
        "last_seen": "2026-09-05T12:00:00Z",
        "next_step": "Check <source>",
        "finding_key": "fixture",
    }
    rows = list(csv.DictReader(io.StringIO(csv_report([row]).decode())))
    assert rows[0]["store"].startswith("'=")
    html = html_report([row]).decode()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "129.95" in html and "12995" not in html
    assert pdf_report([row]).startswith(b"%PDF-")
