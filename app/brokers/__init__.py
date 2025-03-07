# app/brokers/__init__.py
"""Broker adapters. Import the interface from here; concrete adapters are wired at the
edges (main.py, scenarios.py) so the engine stays broker-agnostic."""

from .base import (
    Broker,
    BrokerError,
    OrderAck,
    OrderRejected,
    PnlSnapshot,
    TransientBrokerError,
)

__all__ = [
    "Broker",
    "BrokerError",
    "OrderAck",
    "OrderRejected",
    "PnlSnapshot",
    "TransientBrokerError",
    "get_broker",
]


def get_broker(name: str = "mock", **kwargs) -> Broker:
    """Factory used by main/scenarios. Only 'mock' ships; real adapters register here."""
    key = (name or "mock").lower()
    if key in ("mock", "paper", "MOCK".lower()):
        from .mock import MockBroker

        return MockBroker(**kwargs)
    raise ValueError(f"unknown broker adapter: {name!r} (only 'mock' is available)")
