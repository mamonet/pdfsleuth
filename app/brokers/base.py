# app/brokers/base.py
"""Broker adapter interface.

Every adapter (mock, Lightspeed Connect, whatever comes next) implements this and nothing
else is allowed to leak upward. The engine imports this module, never a concrete adapter.

Contract a real adapter must honour
-----------------------------------
1. Decimal only. Quantities, prices and fees cross this boundary as Decimal. Parse broker
   JSON with Decimal(str(value)); never let a float in.
2. Idempotency. submit() must pass order.client_order_id to the venue as the client order
   id. Re-submitting the same client_order_id must NOT create a second order: return the
   ack for the existing one. If the venue has no such support, the adapter keeps its own
   client_order_id -> broker_order_id map and short-circuits.
3. Error classification is the adapter's job, not the engine's.
     - TransientBrokerError: connection resets, timeouts, 5xx, rate limits. Retryable.
     - OrderRejected: the venue said no (bad symbol, no buying power, halted). Terminal,
       never retried.
     - BrokerError: anything else unexpected.
   Getting this wrong is how a reject turns into a retry storm.
4. stream_fills() is at-least-once. It may replay after a reconnect, so every fill carries
   broker_exec_id and the engine dedupes on it. Never invent an exec id, and never reuse
   one for a different execution.
5. Terminal states are only trusted from the broker. The engine marks an order terminal
   when query_order()/stream_fills() confirms it, not when it assumes completion.
6. positions() and pnl_snapshot() are the reconciliation target: report exactly what the
   venue reports, unadjusted. Do not "helpfully" normalise fees or marks here, that
   destroys the signal the reconciler exists to find.
7. All methods are async and must not block the event loop. Wrap blocking SDKs in a
   thread executor inside the adapter.

Credentials come from config/env with placeholder defaults. No adapter ships a real
endpoint or key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Optional, Sequence

from ..models import BrokerPosition, Fill, Order, OrderStatus

ZERO = Decimal("0")


class BrokerError(Exception):
    """Base for adapter failures."""


class TransientBrokerError(BrokerError):
    """Retryable: timeout, reset, 5xx, throttle."""


class OrderRejected(BrokerError):
    """Terminal: the venue refused the order. Never retried."""

    def __init__(self, reason: str, client_order_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.client_order_id = client_order_id


@dataclass
class OrderAck:
    """What the venue says about an order right now."""

    broker_order_id: str
    status: OrderStatus
    client_order_id: Optional[str] = None
    filled_qty: Decimal = ZERO
    avg_fill_price: Decimal = ZERO
    reject_reason: Optional[str] = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PnlSnapshot:
    """The broker's own P&L, used as the reconciliation target."""

    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    positions: Sequence[BrokerPosition] = ()
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl


class Broker(ABC):
    """Adapter surface. Six calls, no more."""

    name: str = "abstract"

    @abstractmethod
    async def submit(self, order: Order) -> OrderAck:
        """Send an order. Idempotent on order.client_order_id.

        Raises OrderRejected if the venue refuses, TransientBrokerError if the send
        failed in a way worth retrying.
        """

    @abstractmethod
    async def cancel(self, broker_order_id: str) -> OrderAck:
        """Request cancel. Returns the resulting ack; cancelling an already-terminal
        order is not an error, it returns the terminal ack."""

    @abstractmethod
    async def query_order(self, broker_order_id: str) -> OrderAck:
        """Authoritative current state of one order. Used to confirm terminals."""

    @abstractmethod
    def stream_fills(self) -> AsyncIterator[Fill]:
        """At-least-once fill stream. Async iterator, may reconnect internally and
        replay; consumers dedupe on Fill.broker_exec_id."""

    @abstractmethod
    async def positions(self) -> Sequence[BrokerPosition]:
        """The venue's position book, verbatim."""

    @abstractmethod
    async def pnl_snapshot(self) -> PnlSnapshot:
        """The venue's realized/unrealized P&L, verbatim."""

    async def close(self) -> None:
        """Release sockets/sessions. Default is a no-op."""
        return None
