# tests/test_partials.py
"""Partial-fill accounting in the engine: filled_qty accumulates across fills, status flips
to FILLED only when the order is complete, and the oversell guard rejects a sell larger than
the held position."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.brokers.mock import MockBroker, MockConfig
from app.config import Config
from app.engine import Engine, OversellError
from app.models import OrderStatus, Side
from app.store import Store


def _run(coro):
    return asyncio.run(coro)


def _engine(broker):
    return Engine(broker=broker, config=Config(), store=Store(":memory:").open().init())


async def _drain(broker, engine, n, timeout=2.0):
    stream = broker.stream_fills()
    statuses = []
    order_id = None
    for _ in range(n):
        try:
            fill = await asyncio.wait_for(stream.__anext__(), timeout)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        await engine.on_fill(fill)
        order_id = fill.order_id
        statuses.append(engine.get_order(order_id).status)
    return order_id, statuses


def test_partial_fills_accumulate_then_complete():
    broker = MockBroker(MockConfig(base_prices={"AAPL": Decimal("190.00")}, slices=3))
    engine = _engine(broker)

    async def go():
        order = await engine.submit("AAPL", Side.BUY, Decimal("99"))
        oid, statuses = await _drain(broker, engine, 3)     # 33 + 33 + 33
        await broker.close()
        return order.order_id, statuses

    oid, statuses = _run(go())
    live = engine.get_order(oid)

    assert live.filled_qty == Decimal("99")
    assert live.status is OrderStatus.FILLED
    # partial while incomplete, FILLED only on the final fill
    assert statuses[0] is OrderStatus.PARTIALLY_FILLED
    assert statuses[1] is OrderStatus.PARTIALLY_FILLED
    assert statuses[2] is OrderStatus.FILLED
    assert engine._positions.get("AAPL").qty == Decimal("99")


def test_oversell_guard_rejects():
    broker = MockBroker(MockConfig(base_prices={"AAPL": Decimal("190.00")}, slices=1))
    engine = _engine(broker)

    async def go():
        await engine.submit("AAPL", Side.BUY, Decimal("100"))
        await _drain(broker, engine, 1)
        assert engine._positions.get("AAPL").qty == Decimal("100")

        # selling 150 against a 100 long must be refused before it reaches the broker
        with pytest.raises(OversellError):
            await engine.submit("AAPL", Side.SELL, Decimal("150"))

    _run(go())
    assert engine._positions.get("AAPL").qty == Decimal("100")
