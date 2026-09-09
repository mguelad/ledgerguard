from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from packages.reconciliation_core import (
    Coverage,
    Money,
    Order,
    Payment,
    Refund,
    Snapshot,
    evaluate,
    parse_stripe,
    parse_woo,
)

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
OLD = NOW - timedelta(hours=3)
EUR = Money(12995, "EUR", 2)
ZERO = Money(0, "EUR", 2)


def snapshot(**overrides):
    payment = Payment(
        "pi_one", "acct_one", "test", "succeeded", EUR, EUR, OLD, OLD, OLD, store_hint="store_one", order_hint="1"
    )
    order = Order("1", "store_one", "test", "processing", EUR, ZERO, OLD, OLD, OLD, transaction_id="pi_one")
    defaults = dict(
        organization_id="org_one",
        store_id="store_one",
        account_id="acct_one",
        mode="test",
        now=NOW,
        window_from=NOW - timedelta(days=30),
        payments=(payment,),
        orders=(order,),
        refunds=(),
        coverage=tuple(
            Coverage(k, NOW - timedelta(days=35), NOW - timedelta(minutes=1), NOW)
            for k in ["stripe_payments", "stripe_refunds", "woo_orders", "woo_refunds"]
        ),
    )
    return Snapshot(**(defaults | overrides))


def codes(s):
    return sorted(p.rule for p in evaluate(s).proposals if p.ready)


def test_golden_healthy():
    assert codes(snapshot()) == []


def test_golden_unpaid():
    s = snapshot()
    assert codes(replace(s, orders=(replace(s.orders[0], status="pending"),))) == ["PI-001"]


def test_golden_missing():
    assert codes(snapshot(orders=())) == ["PI-002"]


def test_golden_not_succeeded():
    s = snapshot()
    assert codes(
        replace(
            s, payments=(replace(s.payments[0], status="requires_payment_method", received=ZERO, succeeded_at=None),)
        )
    ) == ["PI-003"]


def test_golden_duplicate_success():
    s = snapshot()
    assert codes(replace(s, payments=s.payments + (replace(s.payments[0], id="pi_two"),))) == ["PI-004"]


def test_golden_amount():
    s = snapshot()
    assert codes(replace(s, orders=(replace(s.orders[0], amount=Money(13000, "EUR", 2)),))) == ["PI-005"]


def test_golden_stripe_refund():
    assert codes(snapshot(refunds=(Refund("re_one", "stripe", "pi_one", EUR, "succeeded", OLD),))) == ["PI-006"]


def test_golden_woo_refund():
    s = snapshot()
    assert codes(replace(s, orders=(replace(s.orders[0], status="refunded", refunded=EUR),))) == ["PI-007"]


def test_golden_partial_refund():
    s = snapshot(refunds=(Refund("re_one", "stripe", "pi_one", Money(3000, "EUR", 2), "succeeded", OLD),))
    assert codes(replace(s, orders=(replace(s.orders[0], refunded=Money(2000, "EUR", 2)),))) == ["PI-008"]


def test_golden_stale():
    s = snapshot(orders=())
    assert codes(
        replace(
            s, coverage=tuple(replace(c, covered_through=OLD) if c.source == "woo_orders" else c for c in s.coverage)
        )
    ) == ["PI-009"]


def test_golden_conflicting_identifiers():
    s = snapshot()
    assert codes(
        replace(
            s,
            orders=(replace(s.orders[0], payment_intent_id="pi_two"),),
            payments=s.payments + (replace(s.payments[0], id="pi_two", order_hint=""),),
        )
    ) == ["PI-010"]


def test_golden_async_processing():
    s = snapshot()
    assert (
        codes(
            replace(
                s,
                orders=(replace(s.orders[0], status="on-hold"),),
                payments=(replace(s.payments[0], status="processing", method="sepa_debit", succeeded_at=None),),
            )
        )
        == []
    )


def test_golden_async_grace_starts_at_success():
    s = snapshot()
    p = replace(s.payments[0], succeeded_at=NOW - timedelta(minutes=15), method="sepa_debit")
    result = evaluate(replace(s, payments=(p,), orders=()))
    assert len(result.proposals) == 1 and not result.proposals[0].ready


def test_golden_failed_refund():
    assert codes(snapshot(refunds=(Refund("re_one", "stripe", "pi_one", EUR, "failed", OLD),))) == []


def test_golden_charge_alias():
    s = snapshot()
    assert (
        codes(
            replace(
                s,
                payments=(replace(s.payments[0], charge_ids=("ch_one",)),),
                orders=(replace(s.orders[0], transaction_id="ch_one"),),
            )
        )
        == []
    )


def test_golden_session_alias():
    s = snapshot()
    assert (
        codes(
            replace(
                s,
                payments=(replace(s.payments[0], session_ids=("cs_test_one",)),),
                orders=(replace(s.orders[0], transaction_id="", session_id="cs_test_one"),),
            )
        )
        == []
    )


def test_golden_authorization_on_hold():
    s = snapshot()
    assert (
        codes(
            replace(
                s,
                payments=(replace(s.payments[0], status="requires_capture", received=ZERO, succeeded_at=None),),
                orders=(replace(s.orders[0], status="on-hold"),),
            )
        )
        == []
    )


def test_golden_authorization_fulfilled():
    s = snapshot()
    assert codes(
        replace(s, payments=(replace(s.payments[0], status="requires_capture", received=ZERO, succeeded_at=None),))
    ) == ["PI-003"]


def test_order_collision():
    s = snapshot()
    assert codes(replace(s, orders=s.orders + (replace(s.orders[0], id="2"),))) == ["PI-010"]


def test_no_cross_mode():
    s = snapshot()
    with pytest.raises(ValueError):
        evaluate(replace(s, payments=(replace(s.payments[0], mode="live"),)))


def test_no_shared_account_guess():
    s = snapshot(orders=())
    result = evaluate(replace(s, payments=(replace(s.payments[0], store_hint=""),)))
    assert not result.proposals and result.warnings


def test_candidate_does_not_suppress():
    s = snapshot()
    result = evaluate(
        replace(
            s,
            orders=(replace(s.orders[0], transaction_id=""),),
            payments=(replace(s.payments[0], order_hint="", store_hint=""),),
        )
    )
    assert [p.rule for p in result.proposals] == ["PI-003"]


def test_initial_incomplete_coverage_blocks():
    s = snapshot(orders=())
    assert codes(replace(s, coverage=tuple(replace(c, complete=False) for c in s.coverage))) == ["PI-009"]


def test_refund_bounds_warn_without_truncation():
    result = evaluate(
        snapshot(refunds=(Refund("re_one", "stripe", "pi_one", Money(13000, "EUR", 2), "succeeded", OLD),))
    )
    assert not result.proposals and "refund_exceeds_payment:1" in result.warnings


def test_future_facts_not_evaluated():
    s = snapshot()
    result = evaluate(replace(s, orders=(replace(s.orders[0], status="pending", changed_at=NOW),)))
    assert not result.proposals and result.blocked_identities


@given(st.integers(min_value=0, max_value=10**12))
def test_money_round_trip(minor):
    money = Money(minor, "EUR", 2)
    assert parse_woo(money.decimal(), "EUR") == money


@pytest.mark.parametrize("value", ["1e2", "1.001", "-1", "+1", "NaN", "1,00", " 1", "01", 1.0, True])
def test_reject_invalid_money(value):
    with pytest.raises(ValueError):
        parse_woo(value, "EUR")


@pytest.mark.parametrize(
    "currency,value,expected", [("JPY", "100", 100), ("EUR", "129.95", 12995), ("KWD", "1.234", 1234)]
)
def test_exponents(currency, value, expected):
    assert parse_woo(value, currency).minor == expected


def test_stripe_legacy_currency():
    assert parse_stripe(500, "isk") == parse_woo("5", "ISK")
    with pytest.raises(ValueError):
        parse_stripe(501, "ugx")


@given(st.permutations([0, 1, 2]))
def test_deterministic_permutations(indices):
    s = snapshot()
    payments = tuple(replace(s.payments[0], id="pi_" + str(i), order_hint="1") for i in indices)
    orders = (replace(s.orders[0], transaction_id="pi_0"),)
    result = evaluate(replace(s, payments=payments, orders=orders))
    baseline = evaluate(replace(s, payments=tuple(sorted(payments, key=lambda p: p.id)), orders=orders))
    assert result == baseline
