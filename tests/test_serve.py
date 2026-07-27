"""Dashboard server tests: real HTTP against an OS-assigned port.

The server is started on port 0 so the OS picks a free one, and stopped via
`shutdown()` in a finally block — no fixed ports, no sleeps.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from plotly.offline import get_plotlyjs

from price_of_ethereum.serve import DashboardHandler, _read_frames
from price_of_ethereum.storage import append_jsonl

BLOCK_ROW = {
    "pair": "ETH/USDC",
    "chain_id": 1,
    "block_number": 21_000_000,
    "block_hash": "0xabc",
    "block_timestamp": 1_730_000_000,
    "mixed_block": False,
    "spot": 2500.0,
    "robust_mid": 2500.5,
    "median_depth": 5000.0,
    "mid_source": "sweep_band",
    "gas_price_wei": "2000000000",
    "search_min": 50.0,
    "search_max": 50_000.0,
    "samples_per_side": 4,
    "duration_ms": 1234,
}


def curve_row(block: int, side: str, notional: float, price: float) -> dict:
    return {
        "kind": "curve",
        "chain_id": 1,
        "block_number": block,
        "pair": "ETH/USDC",
        "side": side,
        "size_numeraire": notional,
        "execution_price": price,
        "impact_pct": 0.01,
        "price_impact_bps": 1.0,
        "protocols": ["uniswap_v3"],
        "n_pools": 1,
        "mixed_block": False,
    }


@pytest.fixture
def recorded(tmp_path: Path) -> tuple[Path, Path]:
    rows_path = tmp_path / "rows.jsonl"
    blocks_path = tmp_path / "blocks.jsonl"
    append_jsonl(
        rows_path,
        [
            curve_row(20_999_999, "buy", 1000.0, 2400.0),
            curve_row(21_000_000, "buy", 1000.0, 2501.0),
            curve_row(21_000_000, "sell", 1000.0, 2499.0),
        ],
    )
    append_jsonl(blocks_path, [{**BLOCK_ROW, "block_number": 20_999_999}, BLOCK_ROW])
    return rows_path, blocks_path


@pytest.fixture
def base_url(recorded: tuple[Path, Path]) -> Iterator[str]:
    rows_path, blocks_path = recorded
    handler = partial(
        DashboardHandler,
        rows_path=rows_path,
        blocks_path=blocks_path,
        title="test dashboard",
        poll_ms=4000,
        plotly_js=get_plotlyjs(),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_read_frames_takes_only_the_latest_block(recorded: tuple[Path, Path]) -> None:
    rows, blocks = _read_frames(*recorded)
    assert rows["block_number"].unique().tolist() == [21_000_000]
    assert len(rows) == 2
    assert len(blocks) == 2  # every block summary is kept for the history charts


def test_read_frames_tolerates_missing_files(tmp_path: Path) -> None:
    rows, blocks = _read_frames(tmp_path / "absent.rows.jsonl", tmp_path / "absent.blocks.jsonl")
    assert rows.empty and blocks.empty


def test_index_serves_the_page(base_url: str) -> None:
    response = httpx.get(base_url + "/", timeout=10.0)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "test dashboard" in response.text
    assert '<script src="plotly.js"></script>' in response.text


def test_data_json_carries_figures_and_header(base_url: str) -> None:
    response = httpx.get(base_url + "/data.json", timeout=10.0)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = json.loads(response.text)
    assert payload["header"]["block_number"] == 21_000_000
    assert payload["provenance"]["blocks_recorded"] == 2
    assert payload["provenance"]["rows_latest_block"] == 2
    assert len(payload["figures"]["cost_curve"]["data"]) == 2  # one trace per side


def test_plotly_bundle_is_served_locally(base_url: str) -> None:
    response = httpx.get(base_url + "/plotly.js", timeout=30.0)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "max-age=86400"
    assert len(response.content) > 1_000_000


def test_unknown_route_is_404(base_url: str) -> None:
    assert httpx.get(base_url + "/nope", timeout=10.0).status_code == 404
