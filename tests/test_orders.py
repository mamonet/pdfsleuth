# tests/test_orders.py
"""Order handling: legal vs illegal state transitions; an idempotent resubmit maps to a
single order; a transient error is retried but a reject is not.

Transitions use app.orders; idempotency uses the engine's real submit path; retry-vs-reject
uses app.retry. All three are the delivered code, not mocks.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.brokers.mock import MockBroker, MockConfig
from app.config import Config
from app.engine import Engine
from app.models import Order, OrderStatus, Side
from app.orders import IllegalTransition, can_transition, transition
from app.retry import RejectError, RetryPolicy, TransientBrokerError, call_with_retry
from app.store import Store


def _run(coro):
    return asyncio.run(coro)


def _order(status: OrderStatus) -> Order:
    o = Order(symbol="AAPL", side=Side.BUY, qty=Decimal("10"))
    o.status = status
    return o


# --- state machine (app.orders) ---------------------------------------------

def test_legal_transitions_allowed():
    legal = [
        (OrderStatus.NEW, OrderStatus.SUBMITTED),
        (OrderStatus.SUBMITTED, OrderStatus.WORKING),
        (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
        (OrderStatus.WORKING, OrderStatus.CANCELLED),
        (OrderStatus.SUBMITTED, OrderStatus.REJECTED),
    ]
    for src, dst in legal:
        assert can_transition(src, dst)
        transition(_order(src), dst)   # must not raise


def test_illegal_transitions_rejected():
    illegal = [
        (OrderStatus.FILLED, OrderStatus.WORKING),
        (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.FILLED, OrderStatus.CANCELLED),
        (OrderStatus.REJECTED, OrderStatus.WORKING),
        (OrderStatus.REJECTED, OrderStatus.FILLED),
        (OrderStatus.CANCELLED, OrderStatus.WORKING),
        (OrderStatus.CANCELLED, OrderStatus.REJECTED),
    ]
    for src, dst in illegal:
        assert not can_transition(src, dst)
        with pytest.raises(IllegalTransition):
            transition(_order(src), dst)


# --- idempotency (engine submit path) ---------------------------------------

def test_idempotent_resubmit_maps_to_one_order():
    broker = MockBroker(MockConfig(base_prices={"AAPL": Decimal("190.00")}, slices=1))
    engine = Engine(broker=broker, config=Config(), store=Store(":memory:").open().init())

    async def go():
        # Same economic intent, same (default) nonce -> same idempotency key.
        o1 = await engine.submit("AAPL", Side.BUY, Decimal("100"))
        o2 = await engine.submit("AAPL", Side.BUY, Decimal("100"))
        return o1, o2, await broker.positions()

    o1, o2, positions = _run(go())
    assert o1.order_id == o2.order_id          # collapsed onto one order
    # broker received exactly one order, so its book shows a single fill's worth
    assert positions[0].qty == Decimal("100")


# --- retry vs reject (app.retry) --------------------------------------------

def test_transient_error_is_retried_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientBrokerError("timeout")
        return "ok"

    out = call_with_retry(flaky, RetryPolicy(max_attempts=5, base_delay=0),
                          sleep=lambda _delay: None)
    assert out == "ok"
    assert calls["n"] == 3                     # two failures, then success


def test_reject_is_not_retried():
    calls = {"n": 0}

    def rejected():
        calls["n"] += 1
        raise RejectError("insufficient buying power")

    with pytest.raises(RejectError):
        call_with_retry(rejected, RetryPolicy(max_attempts=5, base_delay=0),
                        sleep=lambda _delay: None)
    assert calls["n"] == 1                      # tried once, never retried
