"""price-of-ethereum: reproduce marketprice.xyz's block-level price/depth data
from your own local Fynd, for any token pair on any supported chain.

Public API is populated stage by stage (Fynd client first). Import surface is
kept intentionally small and re-exported here.
"""

from __future__ import annotations

from price_of_ethereum.collect import CollectionAbortedError, CollectResult, collect_blocks
from price_of_ethereum.fynd import DUMMY_SENDER, FyndClient, FyndError
from price_of_ethereum.pricing import (
    derive_price_impact_bps,
    execution_price,
    impact_pct,
    robust_mid_from_sides,
)
from price_of_ethereum.sizing import (
    SizedRung,
    SpotProbeError,
    numeraire_grid,
    size_rungs,
    spot_price,
)
from price_of_ethereum.snapshot import Snapshot, SnapshotConfig, collect_snapshot
from price_of_ethereum.storage import append_jsonl, load_jsonl, load_parquet, to_parquet
from price_of_ethereum.sweep import (
    AnchorResult,
    Level,
    SweepPoint,
    anchor_target_from_sweep,
    derive_level_from_sweep,
    sweep_side,
)
from price_of_ethereum.tokens import TokenMeta, resolve_tokens
from price_of_ethereum.tycho import TychoClient, TychoError

__version__ = "0.1.0"

__all__ = [
    "DUMMY_SENDER",
    "AnchorResult",
    "CollectResult",
    "CollectionAbortedError",
    "FyndClient",
    "FyndError",
    "Level",
    "SizedRung",
    "Snapshot",
    "SnapshotConfig",
    "SpotProbeError",
    "SweepPoint",
    "TokenMeta",
    "TychoClient",
    "TychoError",
    "anchor_target_from_sweep",
    "append_jsonl",
    "collect_blocks",
    "collect_snapshot",
    "derive_level_from_sweep",
    "derive_price_impact_bps",
    "execution_price",
    "impact_pct",
    "load_jsonl",
    "load_parquet",
    "numeraire_grid",
    "resolve_tokens",
    "robust_mid_from_sides",
    "size_rungs",
    "spot_price",
    "sweep_side",
    "to_parquet",
]
