# app/main.py
"""FastAPI app: read-only state endpoints.

v1: GET /health, GET /positions, GET /orders. The app holds an engine on app.state; the
engine is duck-typed (list_orders / list_positions) so tests can inject a stub without a
broker or feed. Decimals are serialised as strings to preserve exact values over JSON.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from fastapi import FastAPI, Request

app = FastAPI(title="pnl-reconciler", version="0.1.0")


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
    }


def _position_json(p: Any) -> Dict[str, Any]:
    return {
        "symbol": p.symbol,
        "qty": str(p.qty),
        "avg_cost": str(p.avg_cost),
        "realized_pnl": str(p.realized_pnl),
        "fees_paid": str(p.fees_paid),
    }


def _engine(request: Request):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("engine not initialised")
    return engine


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/positions")
async def positions(request: Request) -> List[Dict[str, Any]]:
    return [_position_json(p) for p in _engine(request).list_positions()]


@app.get("/orders")
async def orders(request: Request) -> List[Dict[str, Any]]:
    return [_order_json(o) for o in _engine(request).list_orders()]
