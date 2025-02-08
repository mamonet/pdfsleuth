# app/config.py
"""Runtime configuration. Every knob that can make our P&L disagree with the broker's
lives here, so a disagreement can be explained by a config diff rather than a code read.

Endpoints and keys come from env with placeholder defaults. No real credentials, ever.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class CostBasisMethod(str, Enum):
    AVERAGE = "AVERAGE"
    FIFO = "FIFO"


class MarkSource(str, Enum):
    FEED_MID = "FEED_MID"      # (bid+ask)/2 from the market data feed
    FEED_LAST = "FEED_LAST"    # last trade print from the feed
    BROKER_LAST = "BROKER_LAST"  # the broker's own mark, whatever they use


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_decimal(key: str, default: str) -> Decimal:
    raw = os.environ.get(key, default)
    try:
        return Decimal(raw)
    except Exception:
        raise ValueError(f"{key} must be a decimal number, got {raw!r}")


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class FeeSchedule:
    """Per-fill cost model. Flat + per-share + bps of notional, then floored/capped.

    Simplified against a real venue schedule but the shape is what matters: fees are
    computed per fill, not per order, because partials each carry their own cost.
    """

    per_order: Decimal = Decimal("0.00")
    per_share: Decimal = Decimal("0.005")
    bps_of_notional: Decimal = Decimal("0")     # 1 bps = 0.0001 of notional
    minimum: Decimal = Decimal("1.00")
    maximum: Decimal = Decimal("100.00")

    def compute(self, qty: Decimal, price: Decimal) -> Decimal:
        notional = qty * price
        fee = self.per_order + (self.per_share * qty)
        fee += notional * (self.bps_of_notional / Decimal("10000"))
        if fee < self.minimum:
            fee = self.minimum
        if fee > self.maximum:
            fee = self.maximum
        return fee


@dataclass(frozen=True)
class ReconcileConfig:
    """Tolerances. Below these a diff is noise; at or above it is a break we report.

    Quantity tolerance is deliberately zero: a share count either matches or it does not.
    """

    qty_tolerance: Decimal = Decimal("0")
    price_tolerance: Decimal = Decimal("0.0001")
    pnl_tolerance: Decimal = Decimal("0.01")
    # Marks older than this are not trusted for an unrealized comparison.
    max_mark_age_seconds: float = 5.0


@dataclass(frozen=True)
class Config:
    cost_basis: CostBasisMethod = CostBasisMethod.AVERAGE
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    mark_source: MarkSource = MarkSource.FEED_MID
    reconcile: ReconcileConfig = field(default_factory=ReconcileConfig)

    # Fees reduce realized P&L. Whether they also capitalise into cost basis is a
    # policy choice; brokers differ, and this is a common source of small breaks.
    fees_in_cost_basis: bool = False

    # Adapter selection + placeholder endpoints. The demo never leaves the mock.
    broker_adapter: str = "mock"
    feed_adapter: str = "mock"
    broker_endpoint: str = "https://broker.invalid/REPLACE_ME"
    broker_api_key: str = "REPLACE_ME"
    feed_endpoint: str = "wss://feed.invalid/REPLACE_ME"
    feed_api_key: str = "REPLACE_ME"

    db_path: str = "./pnl_reconciler.db"
    max_retries: int = 5
    retry_base_delay: float = 0.2

    @property
    def using_live_adapter(self) -> bool:
        return self.broker_adapter != "mock" or self.feed_adapter != "mock"


def load_config() -> Config:
    cfg = Config(
        cost_basis=CostBasisMethod(_env("PNL_COST_BASIS", CostBasisMethod.AVERAGE.value)),
        fees=FeeSchedule(
            per_order=_env_decimal("PNL_FEE_PER_ORDER", "0.00"),
            per_share=_env_decimal("PNL_FEE_PER_SHARE", "0.005"),
            bps_of_notional=_env_decimal("PNL_FEE_BPS", "0"),
            minimum=_env_decimal("PNL_FEE_MIN", "1.00"),
            maximum=_env_decimal("PNL_FEE_MAX", "100.00"),
        ),
        mark_source=MarkSource(_env("PNL_MARK_SOURCE", MarkSource.FEED_MID.value)),
        reconcile=ReconcileConfig(
            qty_tolerance=_env_decimal("PNL_TOL_QTY", "0"),
            price_tolerance=_env_decimal("PNL_TOL_PRICE", "0.0001"),
            pnl_tolerance=_env_decimal("PNL_TOL_PNL", "0.01"),
            max_mark_age_seconds=float(_env("PNL_MAX_MARK_AGE", "5.0")),
        ),
        fees_in_cost_basis=_env_bool("PNL_FEES_IN_COST_BASIS", False),
        broker_adapter=_env("PNL_BROKER", "mock"),
        feed_adapter=_env("PNL_FEED", "mock"),
        broker_endpoint=_env("PNL_BROKER_ENDPOINT", "https://broker.invalid/REPLACE_ME"),
        broker_api_key=_env("PNL_BROKER_API_KEY", "REPLACE_ME"),
        feed_endpoint=_env("PNL_FEED_ENDPOINT", "wss://feed.invalid/REPLACE_ME"),
        feed_api_key=_env("PNL_FEED_API_KEY", "REPLACE_ME"),
        db_path=_env("PNL_DB_PATH", "./pnl_reconciler.db"),
        max_retries=int(_env("PNL_MAX_RETRIES", "5")),
        retry_base_delay=float(_env("PNL_RETRY_BASE_DELAY", "0.2")),
    )
    if cfg.using_live_adapter and cfg.broker_api_key == "REPLACE_ME":
        raise RuntimeError("non-mock adapter selected without credentials in env")
    return cfg
