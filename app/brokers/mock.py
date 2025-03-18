# app/brokers/mock.py
"""Mock broker: immediate full fills. No network, no credentials.

v1: every accepted order fills completely at a deterministic price on submit. Enough to
exercise the happy path end to end.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import AsyncIterator, Dict, Optional, Sequence

from ..models import BrokerPosition, Fill, Order, OrderStatus
from .base import Broker, OrderAck, PnlSnapshot

ZERO = Decimal("0")


class MockBroker(Broker):
    name = "mock"

    def __init__(self, base_prices: Optional[Dict[str, Decimal]] = None, fee_per_share: Decimal = Decimal("0.005")):
        # Deterministic reference prices so scenarios and tests are reproducible.
        self._prices: Dict[str, Decimal] = dict(base_prices or {"AAPL": Decimal("190.00")})
        self._fee_per_share = fee_per_share
        self._orders: Dict[str, OrderAck] = {}          # broker_order_id -> ack
        self._by_client: Dict[str, str] = {}            # client_order_id -> broker_order_id
        self._positions: Dict[str, BrokerPosition] = {}
        self._realized = ZERO
        self._fees = ZERO
        self._fills: "asyncio.Queue[Fill]" = asyncio.Queue()
        self._seq = 0

    def _price_for(self, symbol: str) -> Decimal:
        return self._prices.get(symbol, Decimal("100.00"))

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    async def submit(self, order: Order) -> OrderAck:
        # Idempotency: a repeat client_order_id returns the existing ack, no second order.
        cid = order.client_order_id
        if cid and cid in self._by_client:
            return self._orders[self._by_client[cid]]

        bid = self._next_id("MOCK-ORD")
        price = order.limit_price or self._price_for(order.symbol)
        fee = (order.qty * self._fee_per_share).quantize(Decimal("0.01"))

        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=price,
            fee=fee,
            broker_exec_id=self._next_id("MOCK-EXEC"),
        )
        self._apply_to_book(fill)
        await self._fills.put(fill)

        ack = OrderAck(
            broker_order_id=bid,
            status=OrderStatus.FILLED,
            client_order_id=cid,
            filled_qty=order.qty,
            avg_fill_price=price,
        )
        self._orders[bid] = ack
        if cid:
            self._by_client[cid] = bid
        return ack

    async def cancel(self, broker_order_id: str) -> OrderAck:
        ack = self._orders.get(broker_order_id)
        if ack is None:
            return OrderAck(broker_order_id=broker_order_id, status=OrderStatus.REJECTED,
                            reject_reason="unknown order")
        return ack  # already filled in v1, nothing to cancel

    async def query_order(self, broker_order_id: str) -> OrderAck:
        ack = self._orders.get(broker_order_id)
        if ack is None:
            return OrderAck(broker_order_id=broker_order_id, status=OrderStatus.REJECTED,
                            reject_reason="unknown order")
        return ack

    async def stream_fills(self) -> AsyncIterator[Fill]:
        while True:
            fill = await self._fills.get()
            yield fill

    async def positions(self) -> Sequence[BrokerPosition]:
        return [p for p in self._positions.values() if p.qty != ZERO]

    async def pnl_snapshot(self) -> PnlSnapshot:
        book = [p for p in self._positions.values() if p.qty != ZERO]
        unreal = sum(((self._price_for(p.symbol) - p.avg_cost) * p.qty for p in book), ZERO)
        return PnlSnapshot(realized_pnl=self._realized, unrealized_pnl=unreal,
                           fees=self._fees, positions=list(book))

    def _apply_to_book(self, fill: Fill) -> None:
        """Broker-side position/PNL accounting, average cost."""
        self._fees += fill.fee
        pos = self._positions.setdefault(fill.symbol, BrokerPosition(fill.symbol, ZERO, ZERO))
        signed = fill.signed_qty
        if pos.qty == ZERO or (pos.qty > ZERO) == (signed > ZERO):
            # opening or adding
            new_qty = pos.qty + signed
            if new_qty != ZERO:
                pos.avg_cost = (pos.avg_cost * pos.qty + fill.price * signed) / new_qty
            pos.qty = new_qty
        else:
            # reducing/closing: realize against avg_cost
            closing = min(abs(signed), abs(pos.qty))
            direction = 1 if pos.qty > ZERO else -1
            self._realized += (fill.price - pos.avg_cost) * closing * direction
            pos.qty += signed
            if pos.qty == ZERO:
                pos.avg_cost = ZERO
        pos.realized_pnl = self._realized
