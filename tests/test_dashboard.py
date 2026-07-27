"""Dashboard tests: figure construction, payload shape, and report rendering.

Rows come from a real snapshot over the AMM simulator, so the figures are built
from the same column set production emits.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

import amm_sim
from price_of_ethereum import FyndClient, SnapshotConfig, TokenMeta, collect_snapshot
from price_of_ethereum.dashboard import (
    build_payload,
    cost_curve_figure,
    render_page,
    spread_curve_figure,
    write_report,
)

WETH = TokenMeta(address=amm_sim.WETH_ADDRESS, symbol="WETH", decimals=18, quality=100, tax=0)
USDC = TokenMeta(address=amm_sim.USDC_ADDRESS, symbol="USDC", decimals=6, quality=100, tax=0)


@pytest.fixture(scope="module")
def measured() -> tuple[pd.DataFrame, pd.DataFrame]:
    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        return httpx.Response(200, json=amm_sim.quote_response(order["token_in"], order["amount"]))

    config = SnapshotConfig(
        token=WETH,
        numeraire=USDC,
        pair="ETH/USDC",
        chain_id=1,
        search_min=50.0,
        search_max=50_000.0,
        samples_per_side=12,
        impact_levels=(1.0,),
        anchor_targets=(1.0,),
        max_workers=2,
    )
    with FyndClient(transport=httpx.MockTransport(handler)) as fynd:
        snapshot = collect_snapshot(fynd, config)
    rows = pd.DataFrame.from_records(snapshot.to_rows())
    blocks = pd.DataFrame.from_records([snapshot.to_block_row()])
    return rows, blocks


def test_cost_curve_has_a_trace_per_side_and_kind(measured) -> None:
    rows, _ = measured
    figure = cost_curve_figure(rows)
    names = {trace.name for trace in figure.data}
    assert names == {"buy (sweep)", "sell (sweep)", "buy (anchor)", "sell (anchor)"}
    assert figure.layout.xaxis.type == "log"


def test_spread_curve_uses_matched_notionals(measured) -> None:
    rows, blocks = measured
    robust_mid = blocks.iloc[0]["robust_mid"]
    figure = spread_curve_figure(rows, robust_mid)
    assert len(figure.data) == 1
    # Both sides quote the same grid, so every rung pairs.
    curve = rows[rows["kind"] == "curve"]
    assert len(figure.data[0].x) == len(curve[curve["side"] == "buy"])
    assert figure.layout.yaxis.title.text == "spread (bps of mid)"


def test_spread_curve_empty_without_a_mid(measured) -> None:
    rows, _ = measured
    assert spread_curve_figure(rows, None).data == ()


def test_payload_is_json_serializable_and_complete(measured) -> None:
    rows, blocks = measured
    payload = build_payload(rows, blocks)
    encoded = json.dumps(payload)  # must survive the wire without custom encoders
    assert len(encoded) > 0
    assert set(payload["figures"]) == {
        "cost_curve",
        "book_map",
        "spread_curve",
        "history_mid",
        "history_depth",
        "history_health",
    }
    assert payload["header"]["block_number"] == amm_sim.BLOCK_NUMBER
    assert payload["header"]["mid_source"] == "sweep_band"
    assert payload["provenance"]["blocks_recorded"] == 1
    assert payload["provenance"]["mixed_blocks"] == 0
    assert payload["provenance"]["degraded_mids"] == 0
    levels = payload["levels"]
    assert len(levels) == 2  # one per side at the single anchor target
    assert {level["side"] for level in levels} == {"buy", "sell"}
    assert all(level["target_impact_pct"] == 1.0 for level in levels)


def test_payload_tolerates_no_data() -> None:
    payload = build_payload(pd.DataFrame(), pd.DataFrame())
    assert payload["header"] == {}
    assert payload["levels"] == []
    assert payload["provenance"]["blocks_recorded"] == 0
    json.dumps(payload)


def test_render_page_serve_mode_links_bundle_and_polls() -> None:
    page = render_page(title="t", poll_ms=4000, payload=None, inline_js=False)
    assert '<script src="plotly.js"></script>' in page
    assert "const POLL_MS = 4000;" in page
    assert "const EMBEDDED = null;" in page


def test_write_report_is_self_contained(tmp_path: Path, measured) -> None:
    rows, blocks = measured
    path = write_report(tmp_path / "nested" / "report.html", rows, blocks)
    page = path.read_text(encoding="utf-8")
    assert "const POLL_MS = 0;" in page  # frozen: no refresh
    assert "Plotly.react" in page
    assert path.stat().st_size > 3_000_000  # the bundle really is embedded
    assert "ETH/USDC" in page
    # Nothing the document loads comes from off-machine: no external script or
    # stylesheet references at all. (The Plotly bundle mentions cdn.plot.ly as a
    # default topojson config value; scatter/bar charts never request it.)
    assert "<script src=" not in page
    assert "<link " not in page
