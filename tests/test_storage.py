"""Storage tests: JSONL append/load semantics and parquet round-trips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from price_of_ethereum.storage import append_jsonl, load_jsonl, load_parquet, to_parquet

ROWS = [
    {"kind": "curve", "block_number": 1, "execution_price": 2500.5, "protocols": ["uniswap_v3"]},
    {"kind": "anchor", "block_number": 1, "execution_price": 2501.25, "protocols": []},
]


def test_append_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "rows.jsonl"
    assert append_jsonl(path, ROWS) == 2
    frame = load_jsonl(path)
    assert len(frame) == 2
    assert frame["execution_price"].tolist() == [2500.5, 2501.25]
    assert frame["protocols"].tolist() == [["uniswap_v3"], []]


def test_append_accumulates_across_calls(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    append_jsonl(path, ROWS)
    append_jsonl(path, [{"kind": "curve", "block_number": 2}])
    assert len(load_jsonl(path)) == 3


def test_torn_final_line_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "rows.jsonl"
    append_jsonl(path, ROWS)
    with path.open("a") as handle:
        handle.write('{"kind": "curve", "block_num')  # crash mid-append
    with caplog.at_level("WARNING"):
        frame = load_jsonl(path)
    assert len(frame) == 2
    assert "torn final line" in caplog.text


def test_empty_file_loads_as_empty_frame(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.touch()
    assert len(load_jsonl(path)) == 0


def test_whitespace_only_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text('{"ok": 1}\n   \n{"ok": 2}\n')
    assert load_jsonl(path)["ok"].tolist() == [1, 2]


def test_single_torn_line_file_loads_as_empty_frame(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text('{"torn')
    with caplog.at_level("WARNING"):
        frame = load_jsonl(path)
    assert len(frame) == 0
    assert "torn final line" in caplog.text


def test_torn_middle_line_raises(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text('{"ok": 1}\n{"torn\n{"ok": 2}\n')
    with pytest.raises(json.JSONDecodeError):
        load_jsonl(path)


def test_parquet_round_trip(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "rows.jsonl"
    parquet_path = tmp_path / "rows.parquet"
    append_jsonl(jsonl_path, ROWS)
    frame = load_jsonl(jsonl_path)
    to_parquet(frame, parquet_path)
    reloaded = load_parquet(parquet_path)
    assert reloaded["execution_price"].tolist() == frame["execution_price"].tolist()
    assert [list(value) for value in reloaded["protocols"]] == frame["protocols"].tolist()
