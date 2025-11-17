# app/scenarios.py
"""Runnable demo scenarios. Each drives the real engine against the mock broker and prints
the reconciliation report computed at runtime.

    python -m app.scenarios --scenario clean
    python -m app.scenarios --scenario partials
    python -m app.scenarios --scenario reject
    python -m app.scenarios --scenario mismatch

Everything runs against the mock adapters, no network, no credentials. The report is
computed from real values each run; nothing is hard-coded. The mismatch scenario injects
broker drift so the reconciler has a genuine break to locate and classify.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from typing import List

from app.brokers.base import OrderRejected
from app.brokers.mock import DriftConfig, MockBroker, MockConfig
from app.config import Config, MarkSource
from app.engine import Engine
from app.models import Fill, Side
from app.store import Store

BASE = Decimal("190.00")
SYMBOL = "AAPL"


def _engine(broker) -> Engine:
    # Default config: AVERAGE cost basis, FEED_MID marks, mock adapters.
    return Engine(broker=broker, config=Config(), store=Store(":memory:").open().init())


async def _drain(broker: MockBroker, engine: Engine, n: int, timeout: float = 2.0) -> int:
    """Feed exactly n broker fills into the engine (or stop on timeout)."""
    stream = broker.stream_fills()
    got = 0
    for _ in range(n):
        try:
            fill: Fill = await asyncio.wait_for(stream.__anext__(), timeout)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        await engine.on_fill(fill)
        got += 1
    return got


def _mark(engine: Engine, price: Decimal) -> None:
    # Push a mark so unrealized P&L is defined and both sides mark to the same price.
    engine._pnl.marks.update(SYMBOL, price, MarkSource.FEED_MID)


async def _finalise(engine: Engine, broker: MockBroker):
    snapshot = await broker.pnl_snapshot()
    return engine.on_broker_snapshot(snapshot)


async def scenario_clean():
    broker = MockBroker(MockConfig(base_prices={SYMBOL: BASE}, slices=1))
    engine = _engine(broker)
    await engine.submit(SYMBOL, Side.BUY, Decimal("100"))
    await _drain(broker, engine, 1)
    _mark(engine, BASE)
    return await _finalise(engine, broker)


async def scenario_partials():
    broker = MockBroker(MockConfig(base_prices={SYMBOL: BASE}, slices=3, tick=0.0))
    engine = _engine(broker)
    await engine.submit(SYMBOL, Side.BUY, Decimal("99"))
    await _drain(broker, engine, 3)
    await broker.close()
    _mark(engine, BASE)
    return await _finalise(engine, broker)


async def scenario_reject():
    broker = MockBroker(MockConfig(base_prices={SYMBOL: BASE}, reject_symbols=("HALT",)))
    engine = _engine(broker)
    try:
        await engine.submit("HALT", Side.BUY, Decimal("10"))
    except OrderRejected as exc:
        print(f"order rejected as expected: {exc.reason}")
    # No fills, no position: reconcile is trivially clean.
    return await _finalise(engine, broker)


async def scenario_mismatch():
    # Broker book skewed vs what its own fills imply: avg_cost drift on the symbol.
    drift = DriftConfig(symbol=SYMBOL, avg_delta=Decimal("0.50"))
    broker = MockBroker(MockConfig(base_prices={SYMBOL: BASE}, slices=1, drift=drift))
    engine = _engine(broker)
    await engine.submit(SYMBOL, Side.BUY, Decimal("100"))
    await _drain(broker, engine, 1)
    _mark(engine, BASE)
    return await _finalise(engine, broker)


SCENARIOS = {
    "clean": scenario_clean,
    "partials": scenario_partials,
    "reject": scenario_reject,
    "mismatch": scenario_mismatch,
}


async def _run(name: str) -> None:
    result = await SCENARIOS[name]()
    print(f"\n=== scenario: {name} ===")
    print(result.render())
    for line in result.located():
        print(f"  located: {line}")


def main() -> None:
    ap = argparse.ArgumentParser(description="pnl-reconciler demo scenarios")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="clean")
    args = ap.parse_args()
    asyncio.run(_run(args.scenario))


if __name__ == "__main__":
    main()
