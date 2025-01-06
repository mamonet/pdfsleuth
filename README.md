# pnl-reconciler

A small, broker-agnostic engine that executes orders reliably against a broker API and keeps a
running Profit and Loss that reconciles to the broker's own figures. Early scaffold.

Both halves are the same discipline: the engine's idea of a fill, a position, and a P&L must never
quietly drift from the broker's. When it does, this service detects it and says exactly where.

FastAPI (async), SQLite for orders/positions/trade history, a WebSocket feed for marks. Brokers and
feeds sit behind adapter interfaces, so a mock adapter ships for the demo and a real one implements
the same interface without touching the engine.

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

No live money and no live credentials: the demo runs entirely against the mock adapter.
