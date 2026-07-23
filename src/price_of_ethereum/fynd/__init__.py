"""Typed client for a local Fynd DEX-router instance (`/v1/quote` etc.)."""

from __future__ import annotations

from price_of_ethereum.fynd.client import DUMMY_SENDER, FyndClient, FyndError
from price_of_ethereum.fynd.models import (
    BlockInfo,
    EncodingOptions,
    ErrorResponse,
    FeeBreakdown,
    HealthStatus,
    InstanceInfo,
    Order,
    OrderQuote,
    Quote,
    QuoteOptions,
    Route,
    Swap,
    Transaction,
)

__all__ = [
    "DUMMY_SENDER",
    "BlockInfo",
    "EncodingOptions",
    "ErrorResponse",
    "FeeBreakdown",
    "FyndClient",
    "FyndError",
    "HealthStatus",
    "InstanceInfo",
    "Order",
    "OrderQuote",
    "Quote",
    "QuoteOptions",
    "Route",
    "Swap",
    "Transaction",
]
