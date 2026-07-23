"""Append-only JSONL persistence and parquet export.

JSONL is the collection format: one JSON object per line, appended per block.
Writes are buffered, not fsynced — a hard kill can tear the final line or drop
OS-buffered lines entirely, so appends are crash-tolerant, not durable.
`load_jsonl` skips a torn trailing line, and the collector writes each block's
summary only after its rows, making the blocks file the index of blocks whose
rows are fully on disk — join rows against it. Parquet is the analysis format:
convert once, load fast. No database anywhere.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def append_jsonl(path: Path | str, records: Iterable[dict[str, Any]]) -> int:
    """Append `records` to `path`, one JSON object per line; returns the count.

    Creates parent directories on first write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            count += 1
    return count


def load_jsonl(path: Path | str) -> pd.DataFrame:
    """Load a JSONL file into a DataFrame.

    A torn *final* line (crash during the last append) is skipped with a
    warning; a malformed line anywhere else means real corruption and raises.
    The whole file is materialized in memory — fine for block summaries and
    session-sized row files; convert long-running collections to parquet.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                logger.warning("skipping torn final line in %s", path)
                break
            raise
    return pd.DataFrame.from_records(records)


def to_parquet(frame: pd.DataFrame, path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, engine="pyarrow", index=False)


def load_parquet(path: Path | str) -> pd.DataFrame:
    return pd.read_parquet(path, engine="pyarrow")
