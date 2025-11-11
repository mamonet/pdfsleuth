# app/brokers/mock.py
"""Mock broker adapter. No network, no credentials, deterministic.

Builds on the earlier drafts:
  - immediate or sliced partial fills (config: slices)
  - rejects: a symbol on the deny-list, or a submit that trips transient_fails first
  - drift mode: the broker book is deliberately skewed from what its own fills imply, so
    the reconciler has a real discrepancy to locate. Drift is opt-in and only used by the
    mismatch scenario / tests; it never turns on by itself.

Everything crosses the interface as Decimal. Idempotency keyed on client_order_id.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import AsyncIterator, Dict, List, Optional, Sequence

from ..models import BrokerPosition, Fill, Order, OrderStatus
from .base import Broker, OrderAck, OrderRejected, PnlSnapshot, TransientBrokerError

ZERO = Decimal("0")


@dataclass
class DriftConfig:
    """Injected skew applied to the broker snapshot only (not to its fills).

    qty_delta:   add to the reported net qty for `symbol`
    avg_delta:   add to the reported average cost for `symbol`
    realized_delta / unrealized_delta / fee_delta: nudge the reported P&L legs
    This models the real causes the reconciler classifies: a different mark source, a
    different fee treatment, or a fill the engine recorded that the broker book missed.
    """

    symbol: str
    qty_delta: Decimal = ZERO
    avg_delta: Decimal = ZERO
    realized_delta: Decimal = ZERO
    unrealized_delta: Decimal = ZERO
    fee_delta: Decimal = ZERO


@dataclass
class MockConfig:
    base_prices: Dict[str, Decimal] = field(default_factory=lambda: {"AAPL": Decimal("190.00")})
    fee_per_share: Decimal = Decimal("0.005")
    slices: int = 1                      # 1 = immediate full fill; >1 = partials
    tick: float = 0.0                    # seconds between slices
    reject_symbols: Sequence[str] = ()   # submit for these raises OrderRejected
    transient_fails: int = 0             # first N submits raise TransientBrokerError
    drift: Optional[DriftConfig] = None


class MockBroker(Broker):
    name = "mock"

    def __init__(self, config: Optional[MockConfig] = None, **overrides):
        cfg = config or MockConfig()
        # allow flat kwargs (base_prices=..., slices=..., drift=...) for convenience
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
            else:
                raise TypeError(f"unknown MockBroker option: {k!r}")
        self.cfg = cfg
        self._prices: Dict[str, Decimal] = dict(cfg.base_prices)
        self._orders: Dict[str, OrderAck] = {}
        self._by_client: Dict[str, str] = {}
        self._positions: Dict[str, BrokerPosition] = {}
        self._realized = ZERO
        self._fees = ZERO
        self._fills: "asyncio.Queue[Fill]" = asyncio.Queue()
        self._tasks: List[asyncio.Task] = []
        self._transient_left = cfg.transient_fails
        self._seq = 0

    # --- helpers -------------------------------------------------------------

    def _price_for(self, symbol: str) -> Decimal:
        return self._prices.get(symbol, Decimal("100.00"))

    def set_price(self, symbol: str, price: Decimal) -> None:
        self._prices[symbol] = price

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    # --- interface -----------------------------------------------------------

    async def submit(self, order: Order) -> OrderAck:
        cid = order.client_order_id
        if cid and cid in self._by_client:
            return self._orders[self._by_client[cid]]  # idempotent replay

        if order.symbol in self.cfg.reject_symbols:
            raise OrderRejected(f"symbol {order.symbol} not tradable", client_order_id=cid)

        if self._transient_left > 0:
            self._transient_left -= 1
            raise TransientBrokerError("mock transient failure (retry me)")

        bid = self._next_id("MOCK-ORD")
        if self.cfg.slices <= 1:
            ack = await self._fill_immediate(bid, order)
        else:
            ack = OrderAck(broker_order_id=bid, status=OrderStatus.WORKING,
                           client_order_id=cid, filled_qty=ZERO, avg_fill_price=ZERO)
            self._orders[bid] = ack
            self._tasks.append(asyncio.ensure_future(self._drip(bid, order)))
        self._orders.setdefault(bid, ack)
        if cid:
            self._by_client[cid] = bid
        return self._orders[bid]

    async def _fill_immediate(self, bid: str, order: Order) -> OrderAck:
        price = order.limit_price or self._price_for(order.symbol)
        fee = (order.qty * self.cfg.fee_per_share).quantize(Decimal("0.01"))
        fill = Fill(order_id=order.order_id, symbol=order.symbol, side=order.side,
                    qty=order.qty, price=price, fee=fee,
                    broker_exec_id=self._next_id("MOCK-EXEC"))
        self._apply_to_book(fill)
        await self._fills.put(fill)
        ack = OrderAck(broker_order_id=bid, status=OrderStatus.FILLED,
                       client_order_id=order.client_order_id,
                       filled_qty=order.qty, avg_fill_price=price)
        self._orders[bid] = ack
        return ack

    async def _drip(self, bid: str, order: Order) -> None:
        base = self._price_for(order.symbol)
        remaining = order.qty
        slice_qty = (order.qty / self.cfg.slices).quantize(Decimal("0.0001"))
        filled = ZERO
        notional = ZERO
        for i in range(self.cfg.slices):
            if self.cfg.tick:
                await asyncio.sleep(self.cfg.tick)
            qty = remaining if i == self.cfg.slices - 1 else min(slice_qty, remaining)
            if qty <= ZERO:
                break
            price = (order.limit_price or base) + Decimal(i) * Decimal("0.01")
            fee = (qty * self.cfg.fee_per_share).quantize(Decimal("0.01"))
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
            ack = OrderAck(broker_order_id=broker_order_id, status=OrderStatus.CANCELLED,
                           client_order_id=ack.client_order_id, filled_qty=ack.filled_qty,
                           avg_fill_price=ack.avg_fill_price)
            self._orders[broker_order_id] = ack
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
        return self._snapshot_positions()

    async def pnl_snapshot(self) -> PnlSnapshot:
        book = self._snapshot_positions()
        unreal = sum(((self._price_for(p.symbol) - p.avg_cost) * p.qty for p in book), ZERO)
        # Realized is reported NET of fees, the same convention the P&L engine uses, so a
        # matched book reconciles to zero. A broker that netted fees differently is exactly
        # the FEE_TREATMENT break the reconciler is built to surface (see drift.realized_delta).
        realized = self._realized - self._fees
        fees = self._fees
        d = self.cfg.drift
        if d is not None:
            realized += d.realized_delta
            unreal += d.unrealized_delta
            fees += d.fee_delta
        return PnlSnapshot(realized_pnl=realized, unrealized_pnl=unreal, fees=fees,
                           positions=book)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()

    # --- book keeping --------------------------------------------------------

    def _snapshot_positions(self) -> List[BrokerPosition]:
        """The book as reported. Drift is applied here so the fills stay honest but the
        reported snapshot skews, exactly the situation the reconciler must catch."""
        out: List[BrokerPosition] = []
        d = self.cfg.drift
        for p in self._positions.values():
            qty = p.qty
            avg = p.avg_cost
            if d is not None and d.symbol == p.symbol:
                qty = qty + d.qty_delta
                avg = avg + d.avg_delta
            if qty == ZERO:
                continue
            unreal = (self._price_for(p.symbol) - avg) * qty
            out.append(BrokerPosition(symbol=p.symbol, qty=qty, avg_cost=avg,
                                      realized_pnl=p.realized_pnl, unrealized_pnl=unreal))
        return out

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
