# app/brokers/mock.py
"""Mock broker: partial fills over several ticks. No network, no credentials.

v2: an accepted order becomes WORKING, then fills in slices across a few ticks driven by a
background task, so the engine sees PARTIALLY_FILLED before FILLED. Idempotency preserved.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import AsyncIterator, Dict, List, Optional, Sequence

from ..models import BrokerPosition, Fill, Order, OrderStatus
from .base import Broker, OrderAck, PnlSnapshot

ZERO = Decimal("0")


class MockBroker(Broker):
    name = "mock"

    def __init__(
        self,
        base_prices: Optional[Dict[str, Decimal]] = None,
        fee_per_share: Decimal = Decimal("0.005"),
        slices: int = 3,
        tick: float = 0.0,
    ):
        self._prices: Dict[str, Decimal] = dict(base_prices or {"AAPL": Decimal("190.00")})
        self._fee_per_share = fee_per_share
        self._slices = max(1, slices)
        self._tick = tick  # seconds between slices; 0 keeps tests fast
        self._orders: Dict[str, OrderAck] = {}
        self._by_client: Dict[str, str] = {}
        self._order_by_bid: Dict[str, Order] = {}
        self._positions: Dict[str, BrokerPosition] = {}
        self._realized = ZERO
        self._fees = ZERO
        self._fills: "asyncio.Queue[Fill]" = asyncio.Queue()
        self._tasks: List[asyncio.Task] = []
        self._seq = 0

    def _price_for(self, symbol: str) -> Decimal:
        return self._prices.get(symbol, Decimal("100.00"))

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    async def submit(self, order: Order) -> OrderAck:
        cid = order.client_order_id
        if cid and cid in self._by_client:
            return self._orders[self._by_client[cid]]

        bid = self._next_id("MOCK-ORD")
        ack = OrderAck(broker_order_id=bid, status=OrderStatus.WORKING,
                       client_order_id=cid, filled_qty=ZERO, avg_fill_price=ZERO)
        self._orders[bid] = ack
        self._order_by_bid[bid] = order
        if cid:
            self._by_client[cid] = bid
        self._tasks.append(asyncio.ensure_future(self._drip(bid, order)))
        return ack

    async def _drip(self, bid: str, order: Order) -> None:
        """Emit the order's fills as N slices, updating the stored ack each time."""
        base = self._price_for(order.symbol)
        remaining = order.qty
        slice_qty = (order.qty / self._slices).quantize(Decimal("0.0001"))
        filled = ZERO
        notional = ZERO
        for i in range(self._slices):
            if self._tick:
                await asyncio.sleep(self._tick)
            qty = remaining if i == self._slices - 1 else min(slice_qty, remaining)
            if qty <= ZERO:
                break
            # small deterministic price walk so avg differs from a single print
            price = (order.limit_price or base) + Decimal(i) * Decimal("0.01")
            fee = (qty * self._fee_per_share).quantize(Decimal("0.01"))
            fill = Fill(order_id=order.order_id, symbol=order.symbol, side=order.side,
                        qty=qty, price=price, fee=fee,
                        broker_exec_id=self._next_id("MOCK-EXEC"))
            self._apply_to_book(fill)
            await self._fills.put(fill)
            remaining -= qty
            filled += qty
            notional += qty * price
            done = remaining <= ZERO
            self._orders[bid] = OrderAck(
                broker_order_id=bid,
                status=OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED,
                client_order_id=order.client_order_id,
                filled_qty=filled,
                avg_fill_price=(notional / filled) if filled > ZERO else ZERO,
            )
            if done:
                break

    async def cancel(self, broker_order_id: str) -> OrderAck:
        ack = self._orders.get(broker_order_id)
        if ack is None:
            return OrderAck(broker_order_id=broker_order_id, status=OrderStatus.REJECTED,
                            reject_reason="unknown order")
        if ack.status.is_open:
            cancelled = OrderAck(broker_order_id=broker_order_id, status=OrderStatus.CANCELLED,
                                 client_order_id=ack.client_order_id, filled_qty=ack.filled_qty,
                                 avg_fill_price=ack.avg_fill_price)
            self._orders[broker_order_id] = cancelled
            return cancelled
        return ack

    async def query_order(self, broker_order_id: str) -> OrderAck:
        return self._orders.get(
            broker_order_id,
            OrderAck(broker_order_id=broker_order_id, status=OrderStatus.REJECTED,
                     reject_reason="unknown order"),
        )

    async def stream_fills(self) -> AsyncIterator[Fill]:
        while True:
            yield await self._fills.get()

    async def positions(self) -> Sequence[BrokerPosition]:
        return [p for p in self._positions.values() if p.qty != ZERO]

    async def pnl_snapshot(self) -> PnlSnapshot:
        book = [p for p in self._positions.values() if p.qty != ZERO]
        unreal = sum(((self._price_for(p.symbol) - p.avg_cost) * p.qty for p in book), ZERO)
        return PnlSnapshot(realized_pnl=self._realized, unrealized_pnl=unreal,
                           fees=self._fees, positions=list(book))

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()

    def _apply_to_book(self, fill: Fill) -> None:
        self._fees += fill.fee
        pos = self._positions.setdefault(fill.symbol, BrokerPosition(fill.symbol, ZERO, ZERO))
        signed = fill.signed_qty
        if pos.qty == ZERO or (pos.qty > ZERO) == (signed > ZERO):
            new_qty = pos.qty + signed
            if new_qty != ZERO:
                pos.avg_cost = (pos.avg_cost * pos.qty + fill.price * signed) / new_qty
            pos.qty = new_qty
        else:
            closing = min(abs(signed), abs(pos.qty))
            direction = 1 if pos.qty > ZERO else -1
            self._realized += (fill.price - pos.avg_cost) * closing * direction
            pos.qty += signed
            if pos.qty == ZERO:
                pos.avg_cost = ZERO
        pos.realized_pnl = self._realized
