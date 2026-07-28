"""Typed client for a local Fynd DEX-router instance (`/v1/quote` etc.).

Exports the request models a caller builds and every response shape a caller
reads, so user code can be typed against a quote. `EncodingOptions`,
`QuoteOptions` and `ErrorResponse` are assembled inside `FyndClient` and stay
private — every public name here is a compatibility promise once this package
is on PyPI.
"""

from __future__ import annotations

from price_of_ethereum.fynd.client import DUMMY_SENDER, FyndClient, FyndError
from price_of_ethereum.fynd.models import (
    BlockInfo,
    FeeBreakdown,
    HealthStatus,
    InstanceInfo,
    Order,
    OrderQuote,
    Quote,
    Route,
    Swap,
    Transaction,
)

__all__ = [
    "DUMMY_SENDER",
    "BlockInfo",
    "FeeBreakdown",
    "FyndClient",
    "FyndError",
    "HealthStatus",
    "InstanceInfo",
    "Order",
    "OrderQuote",
    "Quote",
    "Route",
    "Swap",
    "Transaction",
]
