# app/feeds/__init__.py
"""Market-data feeds. Import the interface from here; concrete feeds wire at the edges."""

from .base import Feed, Mark

__all__ = ["Feed", "Mark", "get_feed"]


def get_feed(name: str = "mock", **kwargs) -> Feed:
    """Factory used by main/scenarios. Only 'mock' ships; real feeds register here."""
    key = (name or "mock").lower()
    if key in ("mock", "paper"):
        from .mock import MockFeed

        return MockFeed(**kwargs)
    raise ValueError(f"unknown feed adapter: {name!r} (only 'mock' is available)")
