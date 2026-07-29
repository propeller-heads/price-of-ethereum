"""Measure block-level onchain price and depth from your own Fynd instance, for
any token pair on any chain Fynd supports.

Every value this package reports is a Fynd quote or a documented function of
quotes. The names below are the whole public API; submodules are implementation
detail and may move.
"""

from __future__ import annotations

from importlib.metadata import version as _version

from price_of_ethereum.collect import CollectionAbortedError, CollectResult, collect_blocks
from price_of_ethereum.fynd import DUMMY_SENDER, FyndClient, FyndError
from price_of_ethereum.pricing import (
    derive_price_impact_bps,
    execution_price,
    impact_pct,
    robust_mid_from_sides,
)
from price_of_ethereum.sizing import (
    ReferenceRate,
    SizedRung,
    SpotProbeError,
    numeraire_grid,
    reference_rate,
    size_rungs,
    spot_price,
)
from price_of_ethereum.snapshot import Snapshot, SnapshotConfig, collect_snapshot
from price_of_ethereum.storage import append_jsonl, load_jsonl, load_parquet, to_parquet
from price_of_ethereum.sweep import (
    AnchorResult,
    Level,
    MeasuredRung,
    SweepPoint,
    anchor_target_from_sweep,
    derive_level_from_sweep,
    reference_sweep,
    sweep_side,
)
from price_of_ethereum.tokens import TokenMeta, resolve_tokens
from price_of_ethereum.tycho import TychoClient, TychoError

__version__ = _version("price-of-ethereum")

__all__ = [
    "DUMMY_SENDER",
    "AnchorResult",
    "CollectResult",
    "CollectionAbortedError",
    "FyndClient",
    "FyndError",
    "Level",
    "MeasuredRung",
    "ReferenceRate",
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
    "reference_rate",
    "reference_sweep",
    "resolve_tokens",
    "robust_mid_from_sides",
    "size_rungs",
    "spot_price",
    "sweep_side",
    "to_parquet",
]
