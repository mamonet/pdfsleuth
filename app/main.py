# app/main.py
"""FastAPI app: state + reconciliation endpoints, with a lifespan that runs the engine.

Endpoints:
  GET  /health      liveness
  GET  /positions   engine positions
  GET  /orders      engine orders
  GET  /reconcile   engine-vs-broker diff report (computed at request time)
  POST /orders      submit an order through the engine

app.state.engine is a small facade over the real Engine so the HTTP layer never reaches
into engine internals and tests can inject a stub with the same four methods. If no engine
is preset, the lifespan builds the real one against the mock adapters (config from env,
placeholder defaults), replays persisted state, and starts the fill + mark loops. Nothing
here holds a real endpoint or credential.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from .models import Order, OrderType, Side


# --- serialisation -----------------------------------------------------------

def _order_json(o: Any) -> Dict[str, Any]:
    return {
        "order_id": o.order_id,
        "client_order_id": o.client_order_id,
        "broker_order_id": o.broker_order_id,
        "symbol": o.symbol,
        "side": o.side.value,
        "qty": str(o.qty),
        "order_type": o.order_type.value,
        "limit_price": (str(o.limit_price) if o.limit_price is not None else None),
        "status": o.status.value,
        "filled_qty": str(o.filled_qty),
        "avg_fill_price": str(o.avg_fill_price),
        "fees": str(o.fees),
        "reject_reason": o.reject_reason,
        "terminal_confirmed": o.terminal_confirmed,
    }


def _position_json(p: Any) -> Dict[str, Any]:
    return {
        "symbol": p.symbol,
        "qty": str(p.qty),
        "avg_cost": str(p.avg_cost),
        "realized_pnl": str(p.realized_pnl),
        "fees_paid": str(p.fees_paid),
    }


def _report_json(result: Any) -> Dict[str, Any]:
    breaks = [{
        "symbol": b.symbol,
        "leg": b.leg,
        "engine": str(b.engine),
        "broker": str(b.broker),
        "diff": str(b.diff),
        "cause": getattr(getattr(b, "cause", None), "value", None),
    } for b in result.breaks]
    return {
        "clean": result.clean,
        "has_breaks": result.has_breaks,
        "mark_source": getattr(result, "mark_source", None),
        "checked": getattr(result, "checked", None),
        "breaks": breaks,
    }


# --- request model -----------------------------------------------------------

class OrderRequest(BaseModel):
    symbol: str
    side: str
    qty: str
    order_type: str = "MARKET"
    limit_price: Optional[str] = None

    def parsed(self):
        try:
            qty = Decimal(self.qty)
            limit = Decimal(self.limit_price) if self.limit_price is not None else None
        except (InvalidOperation, TypeError):
            raise HTTPException(status_code=422, detail="qty/limit_price must be numeric")
        try:
            side = Side(self.side.upper())
            otype = OrderType(self.order_type.upper())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if qty <= Decimal("0"):
            raise HTTPException(status_code=422, detail="qty must be positive")
        return self.symbol.upper(), side, qty, otype, limit


# --- engine facade -----------------------------------------------------------

class _EngineFacade:
    """Web-facing surface over the real Engine. Keeps HTTP handlers off engine internals."""

    def __init__(self, engine, broker):
        self._engine = engine
        self._broker = broker

    def list_positions(self):
        return [p for p in self._engine._positions.all() if p.qty != Decimal("0")]

    def list_orders(self):
        return list(self._engine._orders.values())

    async def submit(self, symbol, side, qty, order_type, limit_price):
        return await self._engine.submit(symbol, side, qty, order_type, limit_price)

    async def reconcile(self):
        snapshot = await self._broker.pnl_snapshot()
        return self._engine.on_broker_snapshot(snapshot)


def _build():
    """Construct the real engine + adapters against the mock. Lazy imports so the module
    imports cleanly under unit tests that inject a stub engine."""
    import os

    from .brokers import get_broker
    from .config import load_config
    from .engine import Engine
    from .feeds import get_feed
    from .store import Store

    cfg = load_config()   # all env-driven, placeholder defaults, refuses live without creds
    broker = get_broker(cfg.broker_adapter)
    feed = get_feed(cfg.feed_adapter)
    store = Store(cfg.db_path).open().init()
    engine = Engine(broker=broker, config=cfg, store=store)
    engine.load()
    symbols = [s for s in os.getenv("PNL_SYMBOLS", "AAPL").split(",") if s]
    return cfg, broker, feed, store, engine, symbols


async def _consume_fills(broker, engine):
    async for fill in broker.stream_fills():
        try:
            await engine.on_fill(fill)
        except Exception:
            # A stray/duplicate fill must not kill the loop; on_fill dedupes internally.
            continue


async def _consume_marks(feed, engine, mark_source):
    async for mark in feed.stream_marks():
        engine._pnl.marks.update(mark.symbol, mark.price, mark_source)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(app.state, "engine", None) is not None:
        # Preset (tests): do not build or start anything.
        yield
        return

    cfg, broker, feed, store, engine, symbols = _build()
    await feed.subscribe(symbols)
    tasks = [
        asyncio.ensure_future(_consume_fills(broker, engine)),
        asyncio.ensure_future(_consume_marks(feed, engine, cfg.mark_source)),
    ]
    app.state.engine = _EngineFacade(engine, broker)
    app.state._runtime = (broker, feed, store, tasks)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await broker.close()
        await feed.close()
        store.close()


app = FastAPI(title="pnl-reconciler", version="0.1.0", lifespan=lifespan)


def _engine(request: Request):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialised")
    return engine


# --- endpoints ---------------------------------------------------------------

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/positions")
async def positions(request: Request) -> List[Dict[str, Any]]:
    return [_position_json(p) for p in _engine(request).list_positions()]


@app.get("/orders")
async def orders(request: Request) -> List[Dict[str, Any]]:
    return [_order_json(o) for o in _engine(request).list_orders()]


@app.get("/reconcile")
async def reconcile(request: Request) -> Dict[str, Any]:
    return _report_json(await _engine(request).reconcile())


@app.post("/orders")
async def submit_order(request: Request, req: OrderRequest) -> Dict[str, Any]:
    symbol, side, qty, otype, limit = req.parsed()
    result = await _engine(request).submit(symbol, side, qty, otype, limit)
    return _order_json(result)
