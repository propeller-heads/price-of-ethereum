"""Resolve token metadata (decimals, symbol, quality, tax) via Tycho.

Tycho is the sole metadata source — the SDK has no RPC client. Rebasing
(quality 75) and fee-on-transfer (quality 50, or tax > 0) tokens distort measured
price because the amount that reaches the router differs from the amount swapped;
those are warned and flagged rather than silently priced.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from price_of_ethereum.tycho.client import TychoClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenMeta:
    address: str
    symbol: str
    decimals: int
    quality: int  # Tycho quality tier, 0-100: 100 standard, 75 rebasing, 50 fee-on-transfer.
    tax: int  # Transfer tax in basis points.

    @property
    def is_standard(self) -> bool:
        """True for a normal ERC-20 (quality 100, no transfer tax)."""
        return self.quality >= 100 and self.tax == 0


def resolve_tokens(
    client: TychoClient,
    addresses: Sequence[str],
    *,
    min_quality: int | None = None,
) -> dict[str, TokenMeta]:
    """Return metadata for each requested address, keyed by lowercase address.

    Raises `LookupError` if Tycho has no metadata for any requested address —
    the SDK cannot size or price a token whose decimals it doesn't know.
    """
    requested = [address.lower() for address in addresses]
    resolved: dict[str, TokenMeta] = {}
    for token in client.tokens(addresses=list(addresses), min_quality=min_quality):
        meta = TokenMeta(
            address=token.address,
            symbol=token.symbol,
            decimals=token.decimals,
            quality=token.quality,
            tax=token.tax,
        )
        resolved[token.address.lower()] = meta
        if not meta.is_standard:
            logger.warning(
                "token %s (%s) is non-standard (quality=%d tax=%dbps); "
                "measured price may be distorted",
                meta.address,
                meta.symbol,
                meta.quality,
                meta.tax,
            )

    missing = [address for address in requested if address not in resolved]
    if missing:
        raise LookupError(f"Tycho returned no metadata for: {missing}")
    return resolved
