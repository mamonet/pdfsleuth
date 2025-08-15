# app/store.py
"""SQLite persistence. Decimal in, TEXT on disk, Decimal out. No floats near prices.

v2: read/write for orders and fills, and restart recovery that reloads open state so the
engine can resume where it left off.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from .models import Fill, Order, OrderStatus, OrderType, Position, Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    client_order_id TEXT UNIQUE,
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     TEXT,
    status          TEXT NOT NULL,
    filled_qty      TEXT NOT NULL DEFAULT '0',
    avg_fill_price  TEXT NOT NULL DEFAULT '0',
    fees            TEXT NOT NULL DEFAULT '0',
    reject_reason   TEXT,
    terminal_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id        TEXT PRIMARY KEY,
    broker_exec_id TEXT UNIQUE,
    order_id       TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    qty            TEXT NOT NULL,
    price          TEXT NOT NULL,
    fee            TEXT NOT NULL DEFAULT '0',
    ts             TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS positions (
    symbol       TEXT PRIMARY KEY,
    qty          TEXT NOT NULL,
    avg_cost     TEXT NOT NULL,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    fees_paid    TEXT NOT NULL DEFAULT '0',
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_history (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    ref_id    TEXT NOT NULL,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""

OPEN_STATUSES = ("NEW", "SUBMITTED", "WORKING", "PARTIALLY_FILLED")


def _d(v) -> str:
    return str(v)


def _dec(v: Optional[str]) -> Optional[Decimal]:
    return None if v is None else Decimal(v)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None

    def open(self) -> "Store":
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        return self

    def init(self) -> "Store":
        if self.conn is None:
            self.open()
        assert self.conn is not None
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Store":
        return self.open().init()

    def __exit__(self, *exc) -> None:
        self.close()

    # --- writes --------------------------------------------------------------

    def upsert_order(self, o: Order) -> None:
        self.conn.execute(
            """INSERT INTO orders (order_id, client_order_id, broker_order_id, symbol, side,
                    qty, order_type, limit_price, status, filled_qty, avg_fill_price, fees,
                    reject_reason, terminal_confirmed, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
                    broker_order_id=excluded.broker_order_id,
                    status=excluded.status,
                    filled_qty=excluded.filled_qty,
                    avg_fill_price=excluded.avg_fill_price,
                    fees=excluded.fees,
                    reject_reason=excluded.reject_reason,
                    terminal_confirmed=excluded.terminal_confirmed,
                    updated_at=excluded.updated_at""",
            (o.order_id, o.client_order_id, o.broker_order_id, o.symbol, o.side.value,
             _d(o.qty), o.order_type.value, (_d(o.limit_price) if o.limit_price is not None else None),
             o.status.value, _d(o.filled_qty), _d(o.avg_fill_price), _d(o.fees),
             o.reject_reason, int(o.terminal_confirmed), _iso(o.created_at), _iso(o.updated_at)),
        )
        self._log("ORDER", o.order_id, o.status.value)
        self.conn.commit()

    def record_fill(self, f: Fill) -> bool:
        """Insert a fill. Returns False if broker_exec_id already seen (dup replay)."""
        try:
            self.conn.execute(
                """INSERT INTO fills (fill_id, broker_exec_id, order_id, symbol, side, qty,
                        price, fee, ts) VALUES (?,?,?,?,?,?,?,?,?)""",
                (f.fill_id, f.broker_exec_id, f.order_id, f.symbol, f.side.value,
                 _d(f.qty), _d(f.price), _d(f.fee), _iso(f.ts)),
            )
        except sqlite3.IntegrityError:
            return False
        self._log("FILL", f.fill_id, f.broker_exec_id or "")
        self.conn.commit()
        return True

    def upsert_position(self, p: Position) -> None:
        self.conn.execute(
            """INSERT INTO positions (symbol, qty, avg_cost, realized_pnl, fees_paid, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                    qty=excluded.qty, avg_cost=excluded.avg_cost,
                    realized_pnl=excluded.realized_pnl, fees_paid=excluded.fees_paid,
                    updated_at=excluded.updated_at""",
            (p.symbol, _d(p.qty), _d(p.avg_cost), _d(p.realized_pnl), _d(p.fees_paid),
             _iso(p.updated_at)),
        )
        self.conn.commit()

    def _log(self, kind: str, ref_id: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO trade_history (ts, kind, ref_id, detail) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), kind, ref_id, detail),
        )

    # --- reads / recovery ----------------------------------------------------

    def load_orders(self) -> List[Order]:
        return [self._row_to_order(r) for r in self.conn.execute("SELECT * FROM orders")]

    def load_open_orders(self) -> List[Order]:
        q = "SELECT * FROM orders WHERE status IN (%s)" % ",".join("?" * len(OPEN_STATUSES))
        return [self._row_to_order(r) for r in self.conn.execute(q, OPEN_STATUSES)]

    def load_fills(self, order_id: Optional[str] = None) -> List[Fill]:
        if order_id:
            rows = self.conn.execute("SELECT * FROM fills WHERE order_id=? ORDER BY ts", (order_id,))
        else:
            rows = self.conn.execute("SELECT * FROM fills ORDER BY ts")
        return [self._row_to_fill(r) for r in rows]

    def load_positions(self) -> List[Position]:
        return [self._row_to_position(r) for r in self.conn.execute("SELECT * FROM positions")]

    # --- row mapping ---------------------------------------------------------

    @staticmethod
    def _row_to_order(r: sqlite3.Row) -> Order:
        o = Order(
            symbol=r["symbol"], side=Side(r["side"]), qty=Decimal(r["qty"]),
            order_type=OrderType(r["order_type"]),
            limit_price=_dec(r["limit_price"]), order_id=r["order_id"],
            client_order_id=r["client_order_id"], broker_order_id=r["broker_order_id"],
            status=OrderStatus(r["status"]), filled_qty=Decimal(r["filled_qty"]),
            avg_fill_price=Decimal(r["avg_fill_price"]), fees=Decimal(r["fees"]),
            reject_reason=r["reject_reason"], terminal_confirmed=bool(r["terminal_confirmed"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )
        return o

    @staticmethod
    def _row_to_fill(r: sqlite3.Row) -> Fill:
        return Fill(
            order_id=r["order_id"], symbol=r["symbol"], side=Side(r["side"]),
            qty=Decimal(r["qty"]), price=Decimal(r["price"]), fee=Decimal(r["fee"]),
            fill_id=r["fill_id"], broker_exec_id=r["broker_exec_id"],
            ts=datetime.fromisoformat(r["ts"]),
        )

    @staticmethod
    def _row_to_position(r: sqlite3.Row) -> Position:
        return Position(
            symbol=r["symbol"], qty=Decimal(r["qty"]), avg_cost=Decimal(r["avg_cost"]),
            realized_pnl=Decimal(r["realized_pnl"]), fees_paid=Decimal(r["fees_paid"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )
