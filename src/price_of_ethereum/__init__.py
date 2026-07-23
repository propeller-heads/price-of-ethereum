"""price-of-ethereum: reproduce marketprice.xyz's block-level price/depth data
from your own local Fynd, for any token pair on any supported chain.

Public API is populated stage by stage (Fynd client first). Import surface is
kept intentionally small and re-exported here.
"""

from __future__ import annotations

from price_of_ethereum.fynd import DUMMY_SENDER, FyndClient, FyndError

__version__ = "0.1.0"

__all__ = [
    "DUMMY_SENDER",
    "FyndClient",
    "FyndError",
]
