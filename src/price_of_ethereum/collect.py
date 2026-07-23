"""Per-block collection loop: snapshot repeatedly, dedup by majority block,
append rows and block summaries to JSONL.

There is no RPC block clock — a new block is detected by collecting a snapshot
and comparing its majority block to the last recorded one. Snapshots landing on
an already-recorded block are discarded (their quotes are duplicates of what is
already on disk).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from price_of_ethereum.fynd.client import FyndClient
from price_of_ethereum.sizing import SpotProbeError
from price_of_ethereum.snapshot import SnapshotConfig, collect_snapshot
from price_of_ethereum.storage import append_jsonl

logger = logging.getLogger(__name__)


class CollectionAbortedError(RuntimeError):
    """Too many consecutive failed cycles — Fynd is likely down or unsynced."""


@dataclass(frozen=True)
class CollectResult:
    blocks_recorded: int
    rows_written: int
    duplicate_snapshots: int
    failed_cycles: int
    interrupted: bool
    rows_path: Path
    blocks_path: Path


def output_paths(out_dir: Path | str, config: SnapshotConfig) -> tuple[Path, Path]:
    """(rows_path, blocks_path) for a pair/chain, e.g. `eth-usdc_1.rows.jsonl`.

    The pair label is reduced to filesystem-safe characters so it can never
    escape `out_dir` on any platform.
    """
    pair_slug = re.sub(r"[^a-z0-9._-]+", "-", config.pair.lower())
    slug = f"{pair_slug}_{config.chain_id}"
    out_dir = Path(out_dir)
    return out_dir / f"{slug}.rows.jsonl", out_dir / f"{slug}.blocks.jsonl"


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

    A failed cycle (spot probe down, or a snapshot with no block identity at
    all) waits `idle_wait_s` and retries; `max_consecutive_failures` failures in
    a row raise `CollectionAbortedError` so a dead Fynd doesn't spin forever.
    Ctrl-C returns the partial result with `interrupted=True`.

    Rows are appended before the block summary, so a block that appears in the
    summary file has all of its rows on disk; a kill between the two appends
    leaves orphaned rows that a join against the summary file filters out.
    Only the immediately-preceding recorded block is deduplicated — a reorg
    returning to an earlier block number is re-recorded on purpose, because
    post-reorg state is a different measurement.
    """
    rows_path, blocks_path = output_paths(out_dir, config)
    blocks_recorded = rows_written = duplicate_snapshots = failed_cycles = 0
    consecutive_failures = 0
    interrupted = False
    last_block = _last_recorded_block(blocks_path)
    if last_block is not None:
        logger.info("resuming %s after already-recorded block %d", config.pair, last_block)
    try:
        while blocks is None or blocks_recorded < blocks:
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
                duplicate_snapshots += 1
                time.sleep(idle_wait_s)
                continue
            rows = snapshot.to_rows()
            rows_written += append_jsonl(rows_path, rows)
            append_jsonl(blocks_path, [snapshot.to_block_row()])
            blocks_recorded += 1
            last_block = snapshot.block_number
            logger.info(
                "recorded block %d for %s: %d rows, robust_mid=%s (%s), %dms",
                snapshot.block_number,
                config.pair,
                len(rows),
                snapshot.robust_mid,
                snapshot.mid_source,
                snapshot.duration_ms,
            )
    except KeyboardInterrupt:
        interrupted = True
        logger.info("interrupted after %d recorded blocks", blocks_recorded)
    return CollectResult(
        blocks_recorded=blocks_recorded,
        rows_written=rows_written,
        duplicate_snapshots=duplicate_snapshots,
        failed_cycles=failed_cycles,
        interrupted=interrupted,
        rows_path=rows_path,
        blocks_path=blocks_path,
    )
