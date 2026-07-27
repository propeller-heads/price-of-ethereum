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

import io
import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Reverse-read granularity. Lines longer than this still parse; the buffer just
# grows until it finds a newline.
REVERSE_CHUNK_BYTES = 65_536


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


def iter_jsonl_reverse(
    path: Path | str, *, chunk_size: int = REVERSE_CHUNK_BYTES
) -> Iterator[dict[str, Any]]:
    """Yield records newest-first by reading the file backwards.

    Memory stays bounded regardless of file size, so a reader can pull the most
    recent block out of a long collection without parsing everything before it.
    A torn trailing line is skipped like `load_jsonl` does; a decode failure
    further back is real corruption and raises.
    """
    path = Path(path)
    at_trailing_line = True
    with path.open("rb") as handle:
        handle.seek(0, io.SEEK_END)
        remaining = handle.tell()
        pending = b""
        while remaining > 0:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            handle.seek(remaining)
            pending = handle.read(read_size) + pending
            lines = pending.split(b"\n")
            pending = lines.pop(0)
            for line in reversed(lines):
                if not line.strip():
                    continue
                record = _decode_reverse_line(line, path, at_trailing_line)
                at_trailing_line = False
                if record is not None:
                    yield record
        if pending.strip():
            record = _decode_reverse_line(pending, path, at_trailing_line)
            if record is not None:
                yield record


def _decode_reverse_line(line: bytes, path: Path, at_trailing_line: bool) -> dict[str, Any] | None:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        if not at_trailing_line:
            raise
        logger.warning("skipping torn final line in %s", path)
        return None


def load_latest_block_rows(path: Path | str) -> pd.DataFrame:
    """Rows belonging to the newest `block_number` in a rows file.

    Reads backwards and stops at the first record from an older block, so cost
    of a refresh is one block's worth of lines rather than the whole file.
    """
    latest_block: Any = None
    records: list[dict[str, Any]] = []
    for record in iter_jsonl_reverse(path):
        block = record.get("block_number")
        if latest_block is None:
            latest_block = block
        elif block != latest_block:
            break
        records.append(record)
    records.reverse()
    return pd.DataFrame.from_records(records)


def to_parquet(frame: pd.DataFrame, path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, engine="pyarrow", index=False)


def load_parquet(path: Path | str) -> pd.DataFrame:
    return pd.read_parquet(path, engine="pyarrow")
