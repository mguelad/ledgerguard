"""Offline audit entry point. Imports are read in place and never copied or retained."""

import argparse
import csv
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from packages.reconciliation_core import (
    Coverage,
    Order,
    Payment,
    Refund,
    Snapshot,
    evaluate,
    parse_stripe,
    parse_woo,
)
from packages.reconciliation_core.domain import Evaluation, Mode
from packages.reconciliation_core.engine import canonical

from .exports import PLAYBOOKS, csv_report, html_report, pdf_report

MAX_IMPORT = 20 * 1024 * 1024


def date(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("Timestamps must be UTC and end in Z")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def integer(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value):
        raise ValueError("Expected integer minor units")
    return int(value)


def rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    if path.stat().st_size > MAX_IMPORT:
        raise ValueError("Import exceeds 20 MiB")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if (
            not reader.fieldnames
            or len(reader.fieldnames) != len(set(reader.fieldnames))
            or not required.issubset(reader.fieldnames)
        ):
            raise ValueError("CSV header is missing required fields or contains duplicates")
        result: list[dict[str, str]] = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Malformed CSV record")
            if len(result) >= 100000:
                raise ValueError("Import exceeds 100,000 records")
            result.append(row)
        return result


def audit_csv(
    stripe_path: Path,
    woo_path: Path,
    organization: str,
    store: str,
    account: str,
    mode: str,
    as_of: datetime,
    covered_from: datetime,
    covered_through: datetime,
    refund_path: Path | None = None,
) -> Evaluation:
    if mode not in {"test", "live"}:
        raise ValueError("Mode must be test or live")
    if not as_of - timedelta(days=35) <= covered_from < covered_through <= as_of:
        raise ValueError("Invalid audit coverage")
    typed_mode = cast(Mode, mode)
    payments = []
    for r in rows(
        stripe_path,
        {"id", "status", "amount", "amount_received", "currency", "created_at", "changed_at", "account_id", "mode"},
    ):
        if r["account_id"] != account or r["mode"] != mode:
            raise ValueError("Stripe namespace mismatch")
        payments.append(
            Payment(
                r["id"],
                account,
                typed_mode,
                r["status"],
                parse_stripe(integer(r["amount"]), r["currency"]),
                parse_stripe(integer(r["amount_received"]), r["currency"]),
                date(r["created_at"]),
                date(r["changed_at"]),
                date(r["succeeded_at"]) if r.get("succeeded_at") else None,
                r.get("method") or "card",
                r.get("capture_method") or "automatic",
                tuple(filter(None, r.get("charge_id", "").split(";"))),
                tuple(filter(None, r.get("session_id", "").split(";"))),
                r.get("store_id", ""),
                r.get("order_id", ""),
            )
        )
    orders = []
    for r in rows(
        woo_path,
        {
            "id",
            "status",
            "amount",
            "total_refunded",
            "currency",
            "created_at",
            "changed_at",
            "store_id",
            "mode",
            "transaction_id",
        },
    ):
        if r["store_id"] != store or r["mode"] != mode:
            raise ValueError("Woo namespace mismatch")
        orders.append(
            Order(
                r["id"],
                store,
                typed_mode,
                r["status"],
                parse_woo(r["amount"], r["currency"]),
                parse_woo(r["total_refunded"], r["currency"]),
                date(r["created_at"]),
                date(r["changed_at"]),
                date(r["paid_at"]) if r.get("paid_at") else None,
                r.get("payment_method") or "stripe",
                r["transaction_id"],
                r.get("payment_intent_id", ""),
                r.get("charge_id", ""),
                r.get("session_id", ""),
            )
        )
    refunds = []
    if refund_path:
        for r in rows(refund_path, {"id", "source", "parent_id", "amount", "currency", "status", "changed_at"}):
            if r["source"] not in {"stripe", "woo"}:
                raise ValueError("Invalid refund source")
            amount = (
                parse_stripe(integer(r["amount"]), r["currency"])
                if r["source"] == "stripe"
                else parse_woo(r["amount"], r["currency"])
            )
            refunds.append(
                Refund(
                    r["id"],
                    cast(Literal["stripe", "woo"], r["source"]),
                    r["parent_id"],
                    amount,
                    r["status"],
                    date(r["changed_at"]),
                )
            )
    coverage = tuple(
        Coverage(k, covered_from, covered_through, as_of)
        for k in ["stripe_payments", "stripe_refunds", "woo_orders", "woo_refunds"]
    )
    return evaluate(
        Snapshot(
            organization,
            store,
            account,
            typed_mode,
            as_of,
            covered_from,
            tuple(payments),
            tuple(orders),
            tuple(refunds),
            coverage,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile normalized Stripe and Woo exports without cloud credentials."
    )
    for flag in [
        "stripe-csv",
        "woo-csv",
        "organization",
        "store",
        "account",
        "mode",
        "as-of",
        "covered-from",
        "covered-through",
        "output",
    ]:
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--refund-csv")
    args = parser.parse_args()
    try:
        result = audit_csv(
            Path(args.stripe_csv),
            Path(args.woo_csv),
            args.organization,
            args.store,
            args.account,
            args.mode,
            date(args.as_of),
            date(args.covered_from),
            date(args.covered_through),
            Path(args.refund_csv) if args.refund_csv else None,
        )
        directory = Path(args.output)
        directory.mkdir(parents=True, exist_ok=True)
        report_rows = []
        for p in result.proposals:
            report_rows.append(
                {
                    "finding_key": p.key,
                    "rule": p.rule,
                    "severity": p.severity,
                    "state": "open" if p.ready else "provisional",
                    "store": args.store,
                    "mode": args.mode,
                    "amount_minor": p.risk.minor if p.risk else None,
                    "currency": p.risk.currency if p.risk else "",
                    "exponent": p.risk.exponent if p.risk else "",
                    "confidence": p.confidence,
                    "first_seen": args.as_of,
                    "last_seen": args.as_of,
                    "identities": "; ".join(p.identities),
                    "next_step": PLAYBOOKS[p.rule][1],
                }
            )
        (directory / "findings.csv").write_bytes(csv_report(report_rows))
        (directory / "report.html").write_bytes(html_report(report_rows))
        (directory / "report.pdf").write_bytes(pdf_report(report_rows))
        (directory / "evidence.json").write_text(canonical(asdict(result)) + "\n")
        print(f"{len(report_rows)} findings; {len(result.warnings)} data warnings. Reports: {directory}")
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, "Audit rejected: " + str(error) + "\n")


if __name__ == "__main__":
    main()
