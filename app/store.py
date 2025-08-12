# app/store.py
"""SQLite persistence. Money and qty stored as TEXT and round-tripped through Decimal so
no float ever touches a price.

v1: schema + open/init only. Tables: orders, fills, positions, trade_history.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Optional

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
    broker_exec_id TEXT UNIQUE,          -- dedupe replayed streams
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

-- append-only audit of every state change, for reconstruction and debugging
CREATE TABLE IF NOT EXISTS trade_history (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,             -- ORDER | FILL | POSITION
    ref_id    TEXT NOT NULL,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


def _dec(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(value)


class Store:
    """Thin wrapper over a sqlite3 connection. Not thread-safe; one per engine."""

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
