# app/store.py
"""SQLite persistence. Decimal in, TEXT on disk, Decimal out. No floats near prices.

final: atomic writes. A single fill changes three things at once (the fill ledger, the
order's filled_qty/status, the position). Those must land together or not at all, otherwise
a crash mid-write leaves a half-applied fill: money moved on the ledger but the position
never updated, or vice versa. apply_fill() wraps all of it in one transaction, and WAL mode
makes the commit atomic and durable. Recovery reloads open state on restart.
"""

from __future__ import annotations

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
    def __init__(self, path: str = ":memory:", wal: bool = True):
        self.path = path
        self.wal = wal
        self.conn: Optional[sqlite3.Connection] = None

    def open(self) -> "Store":
        # isolation_level="" lets us manage transactions explicitly with BEGIN/COMMIT.
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        if self.wal and self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
        return self

    def init(self) -> "Store":
        if self.conn is None:
            self.open()
        assert self.conn is not None
        self.conn.executescript(SCHEMA)
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Store":
        return self.open().init()

    def __exit__(self, *exc) -> None:
        self.close()

    # --- atomic state changes ------------------------------------------------

    def apply_fill(self, fill: Fill, order: Order, position: Position) -> bool:
        """Record a fill and the order+position it produced, all in one transaction.

        Returns False (and writes nothing) if broker_exec_id was already applied, so a
        replayed stream can call this blindly and stay idempotent. Any error inside rolls
        the whole thing back: no half-applied fill.
        """
        assert self.conn is not None
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE;")
            if fill.broker_exec_id is not None:
                seen = cur.execute(
                    "SELECT 1 FROM fills WHERE broker_exec_id=?", (fill.broker_exec_id,)
                ).fetchone()
                if seen:
                    cur.execute("ROLLBACK;")
                    return False
            self._insert_fill(cur, fill)
            self._write_order(cur, order)
            self._write_position(cur, position)
            self._log(cur, "FILL", fill.fill_id, fill.broker_exec_id or "")
            cur.execute("COMMIT;")
            return True
        except Exception:
            cur.execute("ROLLBACK;")
            raise

    def upsert_order(self, o: Order) -> None:
        """Standalone order write (submit/ack/reject), its own transaction."""
        assert self.conn is not None
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            self._write_order(cur, o)
            self._log(cur, "ORDER", o.order_id, o.status.value)
            cur.execute("COMMIT;")
        except Exception:
            cur.execute("ROLLBACK;")
            raise

    def upsert_position(self, p: Position) -> None:
        assert self.conn is not None
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            self._write_position(cur, p)
            cur.execute("COMMIT;")
        except Exception:
            cur.execute("ROLLBACK;")
            raise

    def record_fill(self, f: Fill) -> bool:
        """Fill-only insert (used when the caller updates order/position separately)."""
        assert self.conn is not None
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            if f.broker_exec_id is not None:
                if cur.execute("SELECT 1 FROM fills WHERE broker_exec_id=?",
                               (f.broker_exec_id,)).fetchone():
                    cur.execute("ROLLBACK;")
                    return False
            self._insert_fill(cur, f)
            self._log(cur, "FILL", f.fill_id, f.broker_exec_id or "")
            cur.execute("COMMIT;")
            return True
        except Exception:
            cur.execute("ROLLBACK;")
            raise

    # --- raw statements (no commit; caller owns the transaction) -------------

    @staticmethod
    def _insert_fill(cur: sqlite3.Cursor, f: Fill) -> None:
        cur.execute(
            """INSERT INTO fills (fill_id, broker_exec_id, order_id, symbol, side, qty,
                    price, fee, ts) VALUES (?,?,?,?,?,?,?,?,?)""",
            (f.fill_id, f.broker_exec_id, f.order_id, f.symbol, f.side.value,
             _d(f.qty), _d(f.price), _d(f.fee), _iso(f.ts)),
        )

    @staticmethod
    def _write_order(cur: sqlite3.Cursor, o: Order) -> None:
        cur.execute(
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
             _d(o.qty), o.order_type.value,
             (_d(o.limit_price) if o.limit_price is not None else None),
             o.status.value, _d(o.filled_qty), _d(o.avg_fill_price), _d(o.fees),
             o.reject_reason, int(o.terminal_confirmed), _iso(o.created_at), _iso(o.updated_at)),
        )

    @staticmethod
    def _write_position(cur: sqlite3.Cursor, p: Position) -> None:
        cur.execute(
            """INSERT INTO positions (symbol, qty, avg_cost, realized_pnl, fees_paid, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                    qty=excluded.qty, avg_cost=excluded.avg_cost,
                    realized_pnl=excluded.realized_pnl, fees_paid=excluded.fees_paid,
                    updated_at=excluded.updated_at""",
            (p.symbol, _d(p.qty), _d(p.avg_cost), _d(p.realized_pnl), _d(p.fees_paid),
             _iso(p.updated_at)),
        )

    @staticmethod
    def _log(cur: sqlite3.Cursor, kind: str, ref_id: str, detail: str) -> None:
        cur.execute(
            "INSERT INTO trade_history (ts, kind, ref_id, detail) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), kind, ref_id, detail),
        )

    # --- reads / recovery ----------------------------------------------------

    def load_orders(self) -> List[Order]:
        return [self._row_to_order(r) for r in self.conn.execute("SELECT * FROM orders")]

    def load_open_orders(self) -> List[Order]:
        q = "SELECT * FROM orders WHERE status IN (%s)" % ",".join("?" * len(OPEN_STATUSES))
        return [self._row_to_order(r) for r in self.conn.execute(q, OPEN_STATUSES)]

    def get_order(self, order_id: str) -> Optional[Order]:
        r = self.conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return self._row_to_order(r) if r else None

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
        return Order(
            symbol=r["symbol"], side=Side(r["side"]), qty=Decimal(r["qty"]),
            order_type=OrderType(r["order_type"]), limit_price=_dec(r["limit_price"]),
            order_id=r["order_id"], client_order_id=r["client_order_id"],
            broker_order_id=r["broker_order_id"], status=OrderStatus(r["status"]),
            filled_qty=Decimal(r["filled_qty"]), avg_fill_price=Decimal(r["avg_fill_price"]),
            fees=Decimal(r["fees"]), reject_reason=r["reject_reason"],
            terminal_confirmed=bool(r["terminal_confirmed"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )

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
