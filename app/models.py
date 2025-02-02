# app/models.py
"""Core value types. Money and quantity are Decimal everywhere, never float."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

ZERO = Decimal("0")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL. Used to turn a fill into a signed qty delta."""
        return 1 if self is Side.BUY else -1

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderStatus(str, Enum):
    NEW = "NEW"                          # created locally, not sent
    SUBMITTED = "SUBMITTED"              # sent, broker has not acked
    WORKING = "WORKING"                  # acked and live at the venue
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass
class Fill:
    """One execution. Immutable once recorded; the fill ledger is append-only."""

    order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal = ZERO
    fill_id: str = field(default_factory=_new_id)
    broker_exec_id: Optional[str] = None   # venue's own id, used to dedupe replayed streams
    ts: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.qty <= ZERO:
            raise ValueError(f"fill qty must be positive, got {self.qty}")
        if self.price < ZERO:
            raise ValueError(f"fill price must be non-negative, got {self.price}")

    @property
    def gross(self) -> Decimal:
        """Notional before fees."""
        return self.qty * self.price

    @property
    def signed_qty(self) -> Decimal:
        return self.qty * self.side.sign


@dataclass
class Order:
    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    order_id: str = field(default_factory=_new_id)
    client_order_id: Optional[str] = None   # our idempotency key, echoed by the broker
    broker_order_id: Optional[str] = None   # assigned on ack
    status: OrderStatus = OrderStatus.NEW
    filled_qty: Decimal = ZERO
    avg_fill_price: Decimal = ZERO
    fees: Decimal = ZERO
    reject_reason: Optional[str] = None
    # Set when the broker itself confirms a terminal state, as opposed to us assuming one.
    terminal_confirmed: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.qty <= ZERO:
            raise ValueError(f"order qty must be positive, got {self.qty}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order requires limit_price")

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def is_complete(self) -> bool:
        return self.filled_qty >= self.qty

    def touch(self) -> None:
        self.updated_at = _now()


@dataclass
class Position:
    """Net position for one symbol. qty is signed: negative means short."""

    symbol: str
    qty: Decimal = ZERO
    avg_cost: Decimal = ZERO       # per-unit cost basis of the open qty
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    updated_at: datetime = field(default_factory=_now)

    @property
    def is_flat(self) -> bool:
        return self.qty == ZERO

    @property
    def is_long(self) -> bool:
        return self.qty > ZERO

    @property
    def is_short(self) -> bool:
        return self.qty < ZERO

    def cost_basis(self) -> Decimal:
        """Signed notional of the open position at cost."""
        return self.qty * self.avg_cost

    def unrealized(self, mark: Decimal) -> Decimal:
        """Works for both sides because qty carries the sign."""
        return (mark - self.avg_cost) * self.qty


@dataclass
class BrokerPosition:
    """The broker's own view, used as the reconciliation target."""

    symbol: str
    qty: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
