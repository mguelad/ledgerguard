"""Immutable inputs and outputs shared by hosted reconciliation and the audit CLI."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from .money import Money

Mode = Literal["test", "live"]
RULE_VERSION = "1.0.0"
MATCH_VERSION = "1.0.0"
RULE_CODES = tuple(f"PI-{n:03}" for n in range(1, 11))


def aware(*values: datetime | None) -> None:
    if any(
        v is not None and (not isinstance(v, datetime) or v.tzinfo is None or v.utcoffset() is None) for v in values
    ):
        raise ValueError("Source timestamps must include a timezone")


def compatible(left: Money, right: Money) -> None:
    if (left.currency, left.exponent) != (right.currency, right.exponent):
        raise ValueError("Amounts on one source object must use the same currency")


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    account_id: str
    mode: Mode
    status: str
    amount: Money
    received: Money
    created_at: datetime
    changed_at: datetime
    succeeded_at: datetime | None = None
    method: str = "card"
    capture_method: str = "automatic"
    charge_ids: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    store_hint: str = ""
    order_hint: str = ""
    observation_id: str = ""

    def __post_init__(self) -> None:
        aware(self.created_at, self.changed_at, self.succeeded_at)
        compatible(self.amount, self.received)
        if self.mode not in {"test", "live"} or self.status not in {
            "requires_payment_method",
            "requires_confirmation",
            "requires_action",
            "processing",
            "requires_capture",
            "canceled",
            "succeeded",
        }:
            raise ValueError("Unsupported payment mode or status")
        if not self.id.startswith("pi_") or not self.account_id.startswith("acct_"):
            raise ValueError("PaymentIntent and account identities are required")


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    store_id: str
    mode: Mode
    status: str
    amount: Money
    refunded: Money
    created_at: datetime
    changed_at: datetime
    paid_at: datetime | None = None
    payment_method: str = "stripe"
    transaction_id: str = ""
    payment_intent_id: str = ""
    charge_id: str = ""
    session_id: str = ""
    observation_id: str = ""

    def __post_init__(self) -> None:
        aware(self.created_at, self.changed_at, self.paid_at)
        compatible(self.amount, self.refunded)
        if self.mode not in {"test", "live"} or self.status not in {
            "pending",
            "processing",
            "on-hold",
            "completed",
            "cancelled",
            "refunded",
            "failed",
            "checkout-draft",
            "trash",
        }:
            raise ValueError("Unsupported order mode or status")
        if not self.id.isascii() or not self.id.isdecimal() or self.id.startswith("0"):
            raise ValueError("Numeric Woo order identity required")


@dataclass(frozen=True, slots=True)
class Refund:
    id: str
    source: Literal["stripe", "woo"]
    parent_id: str
    amount: Money
    status: str
    changed_at: datetime
    observation_id: str = ""

    def __post_init__(self) -> None:
        aware(self.changed_at)
        allowed = (
            {"pending", "requires_action", "succeeded", "failed", "canceled"}
            if self.source == "stripe"
            else {"recorded"}
        )
        if self.source not in {"stripe", "woo"} or self.status not in allowed:
            raise ValueError("Unsupported refund source or status")


@dataclass(frozen=True, slots=True)
class Coverage:
    source: str
    covered_from: datetime
    covered_through: datetime
    observed_at: datetime
    complete: bool = True
    clock_offset_seconds: int = 0

    def __post_init__(self) -> None:
        aware(self.covered_from, self.covered_through, self.observed_at)

    def fresh(self, now: datetime, start: datetime, limit: timedelta) -> bool:
        return (
            self.complete
            and self.covered_from <= start <= self.covered_through <= now
            and timedelta(0) <= now - self.covered_through <= limit
            and timedelta(0) <= now - self.observed_at <= limit
            and abs(self.clock_offset_seconds) <= 300
        )


@dataclass(frozen=True, slots=True)
class Policy:
    version: int = 1
    instant_grace: timedelta = timedelta(minutes=10)
    async_grace: timedelta = timedelta(minutes=30)
    refund_grace: timedelta = timedelta(minutes=60)
    freshness: timedelta = timedelta(minutes=15)
    disabled_rules: tuple[str, ...] = ()
    shadow: bool = False

    def __post_init__(self) -> None:
        for value, low, high in (
            (self.instant_grace, 5, 60),
            (self.async_grace, 10, 120),
            (self.refund_grace, 30, 120),
            (self.freshness, 5, 30),
        ):
            if not timedelta(minutes=low) <= value <= timedelta(minutes=high):
                raise ValueError("Policy exceeds supported bounds")
        if set(self.disabled_rules) - set(RULE_CODES):
            raise ValueError("Unknown rule")


@dataclass(frozen=True, slots=True)
class Snapshot:
    organization_id: str
    store_id: str
    account_id: str
    mode: Mode
    now: datetime
    window_from: datetime
    payments: tuple[Payment, ...]
    orders: tuple[Order, ...]
    refunds: tuple[Refund, ...]
    coverage: tuple[Coverage, ...]
    link_verified: bool = True
    link_conflict: bool = False
    ambiguous_order_hints: tuple[str, ...] = ()
    policy: Policy = field(default_factory=Policy)


@dataclass(frozen=True, slots=True)
class Proposal:
    key: str
    rule: str
    severity: str
    identities: tuple[str, ...]
    risk: Money | None
    confidence: int
    eligible_at: datetime
    ready: bool
    evidence_json: str
    rule_version: str = RULE_VERSION


@dataclass(frozen=True, slots=True)
class Evaluation:
    proposals: tuple[Proposal, ...]
    evaluated_rules: tuple[str, ...]
    blocked_identities: tuple[str, ...]
    warnings: tuple[str, ...]
    input_digest: str
