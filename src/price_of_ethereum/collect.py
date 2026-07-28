"""Per-block collection loop: wait for a new block, snapshot it, append to JSONL.

There is no RPC block clock. Fynd's own view of the chain is the only one that
matters, because a quote is solved against whatever state Fynd holds — an external
block feed can report a block Fynd has not ingested yet. So the clock is a single
quote: `probe_block` asks which block Fynd would answer on, and a full sweep runs
only once that differs from the last recorded block. Waiting therefore costs one
quote per cycle rather than the ~240 a sweep spends.

Block *identity* comes from majority reconciliation across the sweep's own
quotes. The probe decides when to measure, never what to label.

Each block writes three files: `*.rows.jsonl` (every measured rung, flat and
analysis-friendly), `*.blocks.jsonl` (one summary per block), and
`*.anchors.jsonl` (the executable transaction behind each headline level, kept
apart so readers of the first two never page over kilobytes of calldata).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from price_of_ethereum.fynd.client import FyndClient, FyndError
from price_of_ethereum.fynd.models import QUOTE_STATUS_SUCCESS
from price_of_ethereum.sizing import SpotProbeError, atomic
from price_of_ethereum.snapshot import SnapshotConfig, collect_snapshot
from price_of_ethereum.storage import append_jsonl

logger = logging.getLogger(__name__)


class CollectionAbortedError(RuntimeError):
    """Too many consecutive failed cycles — Fynd is likely down or unsynced."""


@dataclass(frozen=True)
class CollectResult:
    blocks_recorded: int
    rows_written: int
    anchors_written: int
    duplicate_snapshots: int
    idle_probes: int
    failed_cycles: int
    interrupted: bool
    rows_path: Path
    blocks_path: Path
    anchors_path: Path


def probe_block(fynd: FyndClient, config: SnapshotConfig) -> int | None:
    """Block Fynd would currently solve against, from one quote.

    Returns None when the probe fails or carries no usable block; callers then
    fall through to a full snapshot rather than stalling on the cheap path. The
    probe reads nothing but the block number, so it never asks for encoding.
    """
    order = fynd.build_order(
        config.numeraire.address,
        config.token.address,
        atomic(config.probe_notional, config.numeraire.decimals),
    )
    try:
        result = fynd.quote(order, min_responses=1, timeout_ms=config.timeout_ms, encoding=False)
    except (FyndError, httpx.HTTPError, ValidationError) as error:
        logger.debug("block probe failed: %s", error)
        return None
    if not result.orders or result.orders[0].status != QUOTE_STATUS_SUCCESS:
        return None
    block_number = result.orders[0].block.number
    return block_number if block_number > 0 else None


def paths_for(out_dir: Path | str, *, pair: str, chain_id: int) -> tuple[Path, Path, Path]:
    """(rows, blocks, anchors) paths for a pair/chain, e.g. `eth-usdc_1.rows.jsonl`.

    The pair label is reduced to filesystem-safe characters so it can never
    escape `out_dir` on any platform.
    """
    pair_slug = re.sub(r"[^a-z0-9._-]+", "-", pair.lower())
    slug = f"{pair_slug}_{chain_id}"
    out_dir = Path(out_dir)
    return (
        out_dir / f"{slug}.rows.jsonl",
        out_dir / f"{slug}.blocks.jsonl",
        out_dir / f"{slug}.anchors.jsonl",
    )


def output_paths(out_dir: Path | str, config: SnapshotConfig) -> tuple[Path, Path, Path]:
    return paths_for(out_dir, pair=config.pair, chain_id=config.chain_id)


def _last_recorded_block(blocks_path: Path) -> int | None:
    """Last block number in an existing summary file, so a restarted collector
    doesn't re-record the block it already has on disk."""
    if not blocks_path.exists():
        return None
    last_block: int | None = None
    with blocks_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            number = record.get("block_number")
            if isinstance(number, int):
                last_block = number
    return last_block


def collect_blocks(
    fynd: FyndClient,
    config: SnapshotConfig,
    *,
    out_dir: Path | str,
    blocks: int | None = None,
    idle_wait_s: float = 2.0,
    max_consecutive_failures: int = 5,
) -> CollectResult:
    """Record `blocks` distinct blocks (None = run until interrupted).

    Each cycle first asks Fynd which block it would answer on (`probe_block`,
    one quote) and waits `idle_wait_s` if that block is already recorded, so an
    idle cycle costs one quote instead of a whole sweep. A probe that fails
    falls through to the full snapshot, which owns the real error handling.

    A failed cycle (spot probe down, or a snapshot with no block identity at
    all) waits `idle_wait_s` and retries; `max_consecutive_failures` failures in
    a row raise `CollectionAbortedError` so a dead Fynd doesn't spin forever.
    Ctrl-C returns the partial result with `interrupted=True`.

    Rows and anchors are appended before the block summary, so a block that
    appears in the summary file has all of its records on disk; a kill between
    the appends leaves orphans that a join against the summary file filters out.
    Only the immediately-preceding recorded block is deduplicated — a reorg
    returning to an earlier block number is re-recorded on purpose, because
    post-reorg state is a different measurement.
    """
    rows_path, blocks_path, anchors_path = output_paths(out_dir, config)
    blocks_recorded = rows_written = anchors_written = 0
    duplicate_snapshots = idle_probes = failed_cycles = 0
    consecutive_failures = 0
    interrupted = False
    last_block = _last_recorded_block(blocks_path)
    if last_block is not None:
        logger.info("resuming %s after already-recorded block %d", config.pair, last_block)
    try:
        while blocks is None or blocks_recorded < blocks:
            # Cheap clock: skip the sweep entirely while the block hasn't moved.
            if last_block is not None and probe_block(fynd, config) == last_block:
                idle_probes += 1
                time.sleep(idle_wait_s)
                continue
            try:
                snapshot = collect_snapshot(fynd, config)
            except SpotProbeError as error:
                failed_cycles += 1
                consecutive_failures += 1
                logger.warning(
                    "collect cycle failed (%d/%d consecutive): %s",
                    consecutive_failures,
                    max_consecutive_failures,
                    error,
                )
                if consecutive_failures >= max_consecutive_failures:
                    raise CollectionAbortedError(
                        f"{consecutive_failures} consecutive failed cycles; last: {error}"
                    ) from error
                time.sleep(idle_wait_s)
                continue
            if snapshot.block_number is None:
                # Spot worked but every sweep quote failed: nothing to label a
                # block with, nothing worth persisting.
                failed_cycles += 1
                consecutive_failures += 1
                logger.warning(
                    "collect cycle produced no block identity (%d/%d consecutive)",
                    consecutive_failures,
                    max_consecutive_failures,
                )
                if consecutive_failures >= max_consecutive_failures:
                    raise CollectionAbortedError(
                        f"{consecutive_failures} consecutive snapshots without block identity"
                    )
                time.sleep(idle_wait_s)
                continue
            consecutive_failures = 0
            if snapshot.block_number == last_block:
                # The block advanced past the probe but the sweep's majority
                # still landed on the recorded one; rare, and its quotes are
                # duplicates of what is already stored.
                duplicate_snapshots += 1
                time.sleep(idle_wait_s)
                continue
            rows = snapshot.to_rows()
            rows_written += append_jsonl(rows_path, rows)
            anchors = snapshot.to_anchor_rows(sender=fynd.sender)
            anchors_written += append_jsonl(anchors_path, anchors)
            append_jsonl(blocks_path, [snapshot.to_block_row()])
            blocks_recorded += 1
            last_block = snapshot.block_number
            logger.info(
                "recorded block %d for %s: %d rows, %d anchors, robust_mid=%s (%s), %dms",
                snapshot.block_number,
                config.pair,
                len(rows),
                len(anchors),
                snapshot.robust_mid,
                snapshot.mid_source,
                snapshot.duration_ms,
            )
            # An anchor-free block means every bisection failed to bracket its
            # target while the bulk sweep still succeeded, which the row and
            # block files cannot show.
            if config.anchor_targets and not anchors:
                logger.warning(
                    "block %d recorded no anchors for %s; %d targets configured",
                    snapshot.block_number,
                    config.pair,
                    len(config.anchor_targets),
                )
    except KeyboardInterrupt:
        interrupted = True
        logger.info("interrupted after %d recorded blocks", blocks_recorded)
    return CollectResult(
        blocks_recorded=blocks_recorded,
        rows_written=rows_written,
        anchors_written=anchors_written,
        duplicate_snapshots=duplicate_snapshots,
        idle_probes=idle_probes,
        failed_cycles=failed_cycles,
        interrupted=interrupted,
        rows_path=rows_path,
        blocks_path=blocks_path,
        anchors_path=anchors_path,
    )
