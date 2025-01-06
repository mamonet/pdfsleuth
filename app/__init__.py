# app/__init__.py
"""pnl-reconciler: broker-agnostic execution + P&L reconciliation engine.

The engine never imports a concrete broker or feed, only the adapter interfaces in
app.brokers.base and app.feeds.base. Wiring happens in app.main / app.scenarios.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
