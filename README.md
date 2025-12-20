# pnl-reconciler

A small, broker-agnostic engine that does two things well: execute orders reliably against a broker
API, and keep a running Profit and Loss that reconciles to the broker's own figures, tick by tick.

Both are the same underlying discipline: the engine's idea of a fill, a position, and a P&L must
never quietly drift from the broker's. When they do, this service detects it and says exactly where.

It runs on the same stack a real desk uses: FastAPI (async), SQLite for orders/positions/trade
history, and a WebSocket market-data feed for marks. Brokers and data feeds are behind small
adapter interfaces, so a mock/paper adapter ships for the demo and a real one (for example a
Lightspeed Connect adapter, or a Polygon.io mark feed) implements the same interface without
touching the engine.

## What it demonstrates

1. **Reliable order execution.** A single order state machine with retries, idempotency, and
   confirmed-terminal handling, so a flaky connection or a duplicate submit never double-sends or
   loses track of an order.
2. **P&L that matches the broker.** Realized and unrealized P&L computed from the fill history, with
   the accounting choices made explicit (cost-basis method, fees, mark source), then reconciled
   against the broker's reported positions and P&L with a diff report.

## Architecture

```
                 WebSocket marks ─────────────┐
                                              v
  Broker adapter  ──fills/positions──>  Engine  ──>  SQLite (orders, fills, positions)
   (mock | real)  <──submit/cancel───   |  |         P&L (realized + unrealized)
                                        |  └──>  Reconciler ──> diff report (engine vs broker)
                                        └──>  FastAPI (state + report endpoints)
```

- **Broker adapter interface:** `submit`, `cancel`, `query_order`, stream `fills`, fetch `positions`
  and the broker's `pnl_snapshot`. A mock adapter simulates fills (including partials and rejects);
  real adapters implement the same five calls.
- **Order state machine:** `NEW -> SUBMITTED -> WORKING -> PARTIALLY_FILLED -> FILLED`, plus
  `CANCELLED` / `REJECTED`. Idempotency keys on submit, retry with backoff, confirmed-terminal
  handling, and an oversell guard.
- **Market data:** a WebSocket feed supplies marks; reconnect-safe, last-good-mark on gaps. The demo
  ships a mock feed; a real feed (e.g. Polygon.io) fits the same interface.
- **P&L engine:** realized and unrealized P&L from the fill ledger. Cost-basis method (average or
  FIFO), commissions/fees, and the mark source (feed mid vs broker last) are all configuration, so
  the computed number can be made to agree with a given broker's convention.
- **Reconciler:** on every fill and on each broker snapshot, compares engine positions and P&L
  against the broker's and emits a diff; anything beyond a set tolerance is flagged.
- **Storage:** SQLite with atomic writes; orders, fills, positions, and trade history are durable and
  survive a restart.
- **Backend:** FastAPI async endpoints for current state and the reconciliation report.

## Run

```bash
pip install -r requirements.txt
# start the engine against the mock broker + mock feed
uvicorn app.main:app --reload
# drive a scenario (fills, partials, a reject) and print the reconciliation report
python -m app.scenarios --scenario partials
```

Scenarios available: `clean`, `partials`, `reject`, `mismatch` (the last deliberately injects a
discrepancy so the reconciler has something to find).

## Tests

```bash
pytest
```

Covers: order-state transitions and idempotent resubmits; partial-fill accounting; average vs FIFO
cost-basis correctness on a known fill sequence; fees applied; and reconciliation diff equals zero on
matched scenarios (and non-zero, correctly located, on a deliberately injected mismatch).

## Reconciliation report (to be filled with real captured output)

> Numbers below come from running the scenarios; they are real captures, not edited.
> Nothing in this table is filled in yet.

| Check | Engine | Broker | Diff |
|-------|--------|--------|------|
| Position (net qty) | _(fill)_ | _(fill)_ | _(fill)_ |
| Average cost | _(fill)_ | _(fill)_ | _(fill)_ |
| Realized P&L | _(fill)_ | _(fill)_ | _(fill)_ |
| Unrealized P&L (mark _(source)_) | _(fill)_ | _(fill)_ | _(fill)_ |

_(attach: report output for a matched run showing zero diff, and one injected-mismatch run showing
the diff located to the exact position/leg)_

## Repository layout

```
pnl-reconciler/
  app/
    main.py            FastAPI app (state + report endpoints)
    engine.py          order state machine + position/P&L engine
    reconcile.py       engine-vs-broker diff
    brokers/           adapter interface + mock adapter (real adapters slot in here)
    feeds/             market-data interface + mock feed
    store.py           SQLite persistence (atomic writes)
    scenarios.py       runnable fill/partial/reject scenarios
  tests/
  requirements.txt
```

## Notes

- Broker- and feed-agnostic by design: the engine never imports a specific broker or feed, only the
  adapter interfaces, so it stays reusable and testable.
- No live money and no live credentials: the demo runs entirely against the mock adapter.
- The value is the reconciliation: a P&L you can prove agrees with the broker, and an execution path
  that stays consistent across disconnects and partial fills.

MIT licensed.
