import json
from datetime import datetime
from pathlib import Path

import pytest

from packages.reconciliation_core import Coverage, Money, Order, Payment, Refund, Snapshot, evaluate

ROOT = Path(__file__).parents[1] / "fixtures/golden"


def source(cls, raw):
    value = dict(raw)
    for key in ["amount", "received", "refunded"]:
        if key in value:
            value[key] = Money(**value[key])
    for key in [
        "created_at",
        "changed_at",
        "succeeded_at",
        "paid_at",
        "covered_from",
        "covered_through",
        "observed_at",
    ]:
        if value.get(key):
            value[key] = datetime.fromisoformat(value[key])
    for key in ["charge_ids", "session_ids"]:
        if key in value:
            value[key] = tuple(value[key])
    return cls(**value)


@pytest.mark.parametrize("path", sorted(ROOT.glob("*.json")), ids=lambda path: path.stem)
def test_portable_golden_contract(path):
    fixture = json.loads(path.read_text())
    value = fixture["input"]
    value["now"] = datetime.fromisoformat(value["now"])
    value["window_from"] = datetime.fromisoformat(value["window_from"])
    for field, cls in [("payments", Payment), ("orders", Order), ("refunds", Refund), ("coverage", Coverage)]:
        value[field] = tuple(source(cls, item) for item in value[field])
    result = evaluate(Snapshot(**value))
    actual = [
        {"rule": p.rule, "ready": p.ready, "risk_minor": p.risk.minor if p.risk else None} for p in result.proposals
    ]
    assert actual == fixture["expected"]


def test_every_rule_has_a_portable_fixture():
    assert len(list(ROOT.glob("*.json"))) >= 20
    rules = {p["rule"] for path in ROOT.glob("*.json") for p in json.loads(path.read_text())["expected"]}
    assert rules == {f"PI-{n:03}" for n in range(1, 11)}
