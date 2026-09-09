"""Deterministic matching and PI-001..PI-010. No I/O or implicit clock."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from .domain import RULE_CODES, Evaluation, Payment, Proposal, Snapshot
from .money import Money

PAID = {"processing", "completed"}
UNPAID = {"pending", "failed", "cancelled", "on-hold"}
ASYNC = {"sepa_debit", "bacs_debit", "au_becs_debit", "us_bank_account", "acss_debit"}
INSTANT = {"card", "card_present", "link", "cashapp", "paypal"}


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v),
        allow_nan=False,
    )


def evaluate(s: Snapshot) -> Evaluation:
    if s.now.tzinfo is None or s.window_from.tzinfo is None or s.now < s.window_from:
        raise ValueError("An explicit UTC evaluation window is required")
    if len({p.id for p in s.payments}) != len(s.payments) or len({o.id for o in s.orders}) != len(s.orders):
        raise ValueError("Duplicate source identities in snapshot")
    if any(p.account_id != s.account_id or p.mode != s.mode for p in s.payments):
        raise ValueError("Payment namespace mismatch")
    if any(o.store_id != s.store_id or o.mode != s.mode for o in s.orders):
        raise ValueError("Order namespace mismatch")
    if s.mode not in {"test", "live"}:
        raise ValueError("Unsupported mode")
    if len({c.source for c in s.coverage}) != len(s.coverage):
        raise ValueError("Duplicate coverage source")
    if len({(r.source, r.id) for r in s.refunds}) != len(s.refunds):
        raise ValueError("Duplicate refund identity")
    ordered_input = asdict(s)
    for key in ("payments", "orders", "refunds", "coverage"):
        ordered_input[key] = sorted(ordered_input[key], key=canonical)
    digest = hashlib.sha256(canonical(ordered_input).encode()).hexdigest()
    proposals: list[Proposal] = []
    blocked: set[str] = set()
    warnings: set[str] = set()
    rules = tuple(r for r in RULE_CODES if r not in s.policy.disabled_rules)
    coverage_evidence = [asdict(c) for c in sorted(s.coverage, key=lambda c: c.source)]

    def emit(
        rule: str, ids: list[str], risk: Money | None, confidence: int, eligible: datetime, facts: dict[str, Any]
    ) -> None:
        if rule not in rules:
            return
        identities = tuple(sorted(set(ids)))
        key = hashlib.sha256(canonical([s.organization_id, s.store_id, s.mode, rule, identities]).encode()).hexdigest()
        evidence = {
            "schema_version": "1.0",
            "rule_version": "1.0.0",
            "match_version": "1.0.0",
            "policy_version": s.policy.version,
            "coverage": coverage_evidence,
            **facts,
        }
        proposals.append(
            Proposal(
                key,
                rule,
                "high" if rule in {"PI-006", "PI-007", "PI-008", "PI-009"} else "critical",
                identities,
                risk,
                confidence,
                eligible,
                s.now >= eligible,
                canonical(evidence),
            )
        )

    required = {"stripe_payments", "stripe_refunds", "woo_orders", "woo_refunds"}
    coverage_by_source = {c.source: c for c in s.coverage}
    stale = sorted(
        k
        for k in required
        if k not in coverage_by_source or not coverage_by_source[k].fresh(s.now, s.window_from, s.policy.freshness)
    )
    if stale:
        emit("PI-009", ["store:" + s.store_id], None, 100, s.now, {"stale_sources": stale})
        return Evaluation(tuple(proposals), tuple(r for r in rules if r == "PI-009"), (), (), digest)
    if not s.link_verified or s.link_conflict:
        if s.link_conflict:
            emit("PI-010", ["store:" + s.store_id], None, 100, s.now, {"reason": "store_account_link_conflict"})
        return Evaluation(
            tuple(proposals), tuple(r for r in rules if r in {"PI-009", "PI-010"}), (), ("link_unverified",), digest
        )

    through = min(coverage_by_source[k].covered_through for k in required)
    payments = {p.id: p for p in s.payments}
    aliases: dict[str, set[str]] = {}
    for p in s.payments:
        for alias in (p.id, *p.charge_ids, *p.session_ids):
            aliases.setdefault(alias, set()).add(p.id)
    hints_by_order: dict[str, list[Payment]] = {}
    for payment in s.payments:
        if payment.order_hint and payment.store_hint in {"", s.store_id}:
            hints_by_order.setdefault(payment.order_hint, []).append(payment)
    matched: dict[str, list[Payment]] = {}
    confidence: dict[str, int] = {}
    claimed: dict[str, list[str]] = {}

    for o in sorted(s.orders, key=lambda x: x.id):
        if not (o.payment_method == "stripe" or o.payment_method.startswith("stripe_")):
            continue
        oid = "order:" + o.id
        refs = tuple(sorted(set(filter(None, (o.transaction_id, o.payment_intent_id, o.charge_id, o.session_id)))))
        resolved: set[str] = set()
        unknown = []
        for ref in refs:
            targets = aliases.get(ref, set())
            if not targets:
                unknown.append(ref)
            resolved.update(targets)
        hints = hints_by_order.get(o.id, [])
        conflict = len(resolved) > 1 or bool(unknown) or (o.id in s.ambiguous_order_hints and bool(hints))
        conflict |= any(
            payments[pid].store_hint not in {"", s.store_id} or payments[pid].order_hint not in {"", o.id}
            for pid in resolved
        )
        if conflict:
            ids = [oid, *("payment:" + pid for pid in resolved)]
            blocked.update(ids)
            emit(
                "PI-010",
                ids,
                o.amount,
                100,
                s.now,
                {
                    "reason": "conflicting_or_unresolved_exact_identifiers",
                    "order": asdict(o),
                    "references": refs,
                    "unresolved": unknown,
                },
            )
            continue
        candidate_map = {pid: payments[pid] for pid in resolved}
        for p in hints:
            candidate_map[p.id] = p
        if refs:
            confidence[o.id] = 100 if any(r.startswith("pi_") for r in refs) else 95
        elif candidate_map:
            confidence[o.id] = 90 if all(p.store_hint == s.store_id for p in candidate_map.values()) else 80
        else:
            confidence[o.id] = 0
        matched[o.id] = sorted(candidate_map.values(), key=lambda p: p.id)
        for pid in candidate_map:
            claimed.setdefault(pid, []).append(o.id)

    for pid, order_ids in sorted(claimed.items()):
        if len(order_ids) > 1:
            ids = ["payment:" + pid, *("order:" + oid for oid in order_ids)]
            blocked.update(ids)
            emit(
                "PI-010",
                ids,
                payments[pid].received,
                100,
                s.now,
                {"reason": "payment_claimed_by_multiple_orders", "payment_id": pid, "order_ids": sorted(order_ids)},
            )

    for o in sorted(s.orders, key=lambda x: x.id):
        oid = "order:" + o.id
        if o.id not in matched or oid in blocked:
            continue
        group = matched[o.id]
        ids = [oid, *("payment:" + p.id for p in group)]
        if o.created_at < s.window_from:
            blocked.update(ids)
            warnings.add("history_outside_coverage:" + oid)
            continue
        if any(i in blocked for i in ids):
            blocked.update(ids)
            continue
        if any(t > through for t in [o.changed_at, *(p.changed_at for p in group)]):
            blocked.update(ids)
            continue
        succeeded = [p for p in group if p.status == "succeeded"]
        facts: dict[str, Any] = {"order": asdict(o), "payments": [asdict(p) for p in group]}
        compensated: set[str] = set()
        for p in succeeded:
            refunds = [r for r in s.refunds if r.source == "stripe" and r.parent_id == p.id and r.status == "succeeded"]
            if (
                refunds
                and p.received.minor > 0
                and all(
                    r.amount.currency == p.received.currency and r.amount.exponent == p.received.exponent
                    for r in refunds
                )
                and sum(r.amount.minor for r in refunds) == p.received.minor
                and all(r.changed_at + s.policy.refund_grace <= min(s.now, through) for r in refunds)
            ):
                compensated.add(p.id)
        # A full return of each excess attempt, with one retained payment, proves correction.
        # These returns reverse duplicate attempts; they do not reduce the retained order's sale.
        corrected_duplicate = len(succeeded) > 1 and len(succeeded) - len(compensated) == 1
        facts["compensated_duplicate_payment_ids"] = sorted(compensated) if corrected_duplicate else []
        if len(succeeded) > 1 and not corrected_duplicate:
            eligible = max((p.succeeded_at or p.changed_at) + _grace(p, s) for p in succeeded)
            emit("PI-004", ids, o.amount, confidence[o.id], eligible, facts)
        if not succeeded and o.status in PAID:
            candidates = sorted(
                p.id
                for p in s.payments
                if p.amount == o.amount and abs((p.created_at - o.created_at).total_seconds()) <= 600
            )
            emit(
                "PI-003",
                [oid],
                o.amount,
                confidence[o.id],
                (o.paid_at or o.changed_at) + s.policy.instant_grace,
                {**facts, "candidate_payment_ids": candidates, "candidate_confidence": 40 if candidates else 0},
            )
        for p in succeeded:
            eligible = (p.succeeded_at or p.changed_at) + _grace(p, s)
            pair = [oid, "payment:" + p.id]
            if o.status in UNPAID:
                emit("PI-001", pair, p.received, confidence[o.id], eligible, facts)
            if p.received != o.amount:
                emit("PI-005", pair, p.received, confidence[o.id], eligible, facts)
        stripe_refunds = sorted(
            (
                r
                for r in s.refunds
                if r.source == "stripe"
                and r.parent_id in {p.id for p in group}
                and r.status == "succeeded"
                and not (corrected_duplicate and r.parent_id in compensated)
            ),
            key=lambda r: r.id,
        )
        woo_refunds = sorted((r for r in s.refunds if r.source == "woo" and r.parent_id == o.id), key=lambda r: r.id)
        if any(r.changed_at > through for r in (*stripe_refunds, *woo_refunds)):
            blocked.add(oid)
            continue
        if any(
            r.amount.currency != o.amount.currency or r.amount.exponent != o.amount.exponent
            for r in (*stripe_refunds, *woo_refunds)
        ):
            warnings.add("refund_currency_conflict:" + o.id)
            emit(
                "PI-008",
                [oid],
                o.refunded,
                confidence[o.id],
                max([o.changed_at, *(r.changed_at for r in (*stripe_refunds, *woo_refunds))]) + s.policy.refund_grace,
                {**facts, "reason": "refund_currency_conflict"},
            )
            continue
        sr = sum(r.amount.minor for r in stripe_refunds)
        wr = o.refunded.minor
        if wr > o.amount.minor or any(
            sum(r.amount.minor for r in stripe_refunds if r.parent_id == p.id) > p.received.minor for p in succeeded
        ):
            warnings.add("refund_exceeds_payment:" + o.id)
            blocked.add(oid)
            continue
        if woo_refunds and sum(r.amount.minor for r in woo_refunds) != wr:
            warnings.add("woo_refund_projection_inconsistent:" + o.id)
            blocked.add(oid)
            continue
        refund_facts = {
            **facts,
            "stripe_refunds": [asdict(r) for r in stripe_refunds],
            "woo_refunds": [asdict(r) for r in woo_refunds],
            "stripe_refunded_minor": sr,
            "woo_refunded_minor": wr,
            "manual_status_possible": o.status == "refunded",
        }
        # Pending or failed Stripe refunds never count as money returned.
        refund_time = max(
            [
                o.changed_at if wr or o.status == "refunded" else s.window_from,
                *(r.changed_at for r in (*stripe_refunds, *woo_refunds)),
            ]
        )
        eligible = refund_time + s.policy.refund_grace
        risk = Money(abs(sr - wr), o.amount.currency, o.amount.exponent)
        if sr and not wr:
            emit("PI-006", [oid], risk, confidence[o.id], eligible, refund_facts)
        elif (wr or o.status == "refunded") and not sr:
            emit("PI-007", [oid], risk if wr else o.amount, confidence[o.id], eligible, refund_facts)
        elif sr != wr:
            emit("PI-008", [oid], risk, confidence[o.id], eligible, refund_facts)

    for p in sorted(s.payments, key=lambda x: x.id):
        pid = "payment:" + p.id
        if p.id in claimed or pid in blocked or p.status != "succeeded":
            continue
        if p.changed_at > through or p.created_at < s.window_from:
            blocked.add(pid)
        elif p.store_hint == s.store_id:
            emit(
                "PI-002",
                [pid],
                p.received,
                90,
                (p.succeeded_at or p.changed_at) + _grace(p, s),
                {"payment": asdict(p), "confirmed_store_id": s.store_id},
            )
        else:
            warnings.add("unattributed_payment:" + p.id)
    return Evaluation(
        tuple(sorted(proposals, key=lambda p: p.key)), rules, tuple(sorted(blocked)), tuple(sorted(warnings)), digest
    )


def _grace(payment: Payment, snapshot: Snapshot) -> timedelta:
    return snapshot.policy.instant_grace if payment.method in INSTANT else snapshot.policy.async_grace
