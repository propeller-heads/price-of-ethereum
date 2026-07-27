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
    PriceView,
    book_map_figure,
    build_payload,
    cost_curve_figure,
    history_mid_figure,
    render_page,
    spread_curve_figure,
    write_report,
)
from price_of_ethereum.pricing import ROBUST_MID_MIN_DEPTH

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
    assert figure.layout.yaxis.title.text == "spread (% of mid)"


def test_axes_are_labelled_unambiguously(measured) -> None:
    rows, blocks = measured
    # Log ticks read one per decade, not Plotly's 2x/5x mantissa labels.
    size_axis = cost_curve_figure(rows).layout.xaxis
    assert size_axis.title.text == "size (numeraire)"
    assert size_axis.type == "log"
    assert size_axis.dtick == 1
    assert size_axis.tickformat == "~s"
    # Block numbers are identifiers: plain integers, never abbreviated.
    block_axis = history_mid_figure(blocks).layout.xaxis
    assert block_axis.type == "linear"
    assert block_axis.tickformat == "d"
    assert block_axis.exponentformat == "none"


def test_level_table_reports_impact_in_percent(measured) -> None:
    rows, blocks = measured
    payload = build_payload(rows, blocks)
    level = payload["levels"][0]
    assert "price_impact_bps" not in level
    anchors = rows[rows["kind"] == "anchor"]
    stored_bps = anchors.sort_values(["target_impact_pct", "side"]).iloc[0]["price_impact_bps"]
    assert level["price_impact_pct"] == pytest.approx(stored_bps / 100.0)
    # Column order is preserved: percent sits where bps did.
    assert list(level).index("price_impact_pct") == list(level).index("impact_pct") + 1


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


class TestPriceView:
    def test_upright_reads_numeraire_per_token(self) -> None:
        view = PriceView(token_symbol="WETH", numeraire_symbol="USDC", mid=2500.0)
        assert view.price_unit == "USDC per WETH"
        assert view.pair_label == "WETH/USDC"
        assert view.size_symbol == "USDC"
        assert view.convert(2500.0) == 2500.0
        assert view.convert_size_value(5000.0) == 5000.0

    def test_inverted_flips_prices_and_restates_sizes(self) -> None:
        view = PriceView(token_symbol="WETH", numeraire_symbol="USDC", inverted=True, mid=2500.0)
        assert view.price_unit == "WETH per USDC"
        assert view.pair_label == "USDC/WETH"
        assert view.size_symbol == "WETH"
        assert view.convert(2500.0) == pytest.approx(1 / 2500.0)
        assert view.convert_size_value(5000.0) == pytest.approx(2.0)

    def test_button_offers_the_other_direction(self) -> None:
        upright = PriceView(token_symbol="WETH", numeraire_symbol="USDC")
        flipped = PriceView(token_symbol="WETH", numeraire_symbol="USDC", inverted=True)
        # The label a click switches TO must never equal what is on screen.
        assert upright.pair_label == "WETH/USDC"
        assert upright.other_pair_label == "USDC/WETH"
        assert flipped.pair_label == "USDC/WETH"
        assert flipped.other_pair_label == "WETH/USDC"

    def test_sizes_stay_in_numeraire_without_a_mid(self) -> None:
        # No mid to divide by, so sizes must not be relabelled as the token.
        view = PriceView(token_symbol="WETH", numeraire_symbol="USDC", inverted=True)
        assert view.sizes_restated is False
        assert view.size_symbol == "USDC"
        assert view.convert_size_value(5000.0) == 5000.0

    def test_convert_guards_none_and_zero(self) -> None:
        view = PriceView(inverted=True, mid=2500.0)
        assert view.convert(None) is None
        assert view.convert(0.0) is None
        assert view.convert_size_value(None) is None


def test_inverted_payload_mirrors_prices_and_units(measured) -> None:
    rows, blocks = measured
    upright = build_payload(rows, blocks)
    flipped = build_payload(rows, blocks, inverted=True)

    mid = upright["header"]["robust_mid"]
    assert upright["view"]["price_unit"] == "USDC per WETH"
    assert flipped["view"]["price_unit"] == "WETH per USDC"
    assert flipped["header"]["robust_mid"] == pytest.approx(1 / mid)
    # Flipping re-denominates the whole view: sizes move to the other token.
    assert upright["view"]["size_symbol"] == "USDC"
    assert flipped["view"]["size_symbol"] == "WETH"
    assert flipped["header"]["median_depth"] == pytest.approx(
        upright["header"]["median_depth"] / mid
    )
    # Impact is a ratio, so the cost curve's y values are direction-independent
    # while its x values follow the size denomination.
    upright_curve = upright["figures"]["cost_curve"]["data"][0]
    flipped_curve = flipped["figures"]["cost_curve"]["data"][0]
    assert flipped_curve["y"] == upright_curve["y"]
    assert flipped_curve["x"] != upright_curve["x"]
    # The anchored level table follows the same direction as the charts.
    upright_level = upright["levels"][0]
    flipped_level = flipped["levels"][0]
    assert flipped_level["execution_price"] == pytest.approx(1 / upright_level["execution_price"])
    assert flipped_level["size_numeraire"] == pytest.approx(upright_level["size_numeraire"] / mid)


def test_book_map_axis_and_mid_line_follow_the_view(measured) -> None:
    rows, blocks = measured
    robust_mid = blocks.iloc[0]["robust_mid"]
    view = PriceView(token_symbol="WETH", numeraire_symbol="USDC", inverted=True, mid=robust_mid)
    figure = book_map_figure(rows, robust_mid, view)
    assert figure.layout.yaxis.title.text == "execution price (WETH per USDC)"
    assert figure.layout.xaxis.title.text == "size (WETH)"
    assert figure.data[0].name == "ask (buy WETH)"
    mid_lines = [shape for shape in figure.layout.shapes if shape.type == "line"]
    assert mid_lines and mid_lines[0].y0 == pytest.approx(1 / robust_mid)
    # The robust-mid band is a pair of notionals, so it moves with the size axis.
    bands = [shape for shape in figure.layout.shapes if shape.type == "rect"]
    assert bands and bands[0].x0 == pytest.approx(ROBUST_MID_MIN_DEPTH / robust_mid)


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
    assert "const EMBEDDED_INVERTED = null;" in page


def test_write_report_is_self_contained(tmp_path: Path, measured) -> None:
    rows, blocks = measured
    path = write_report(tmp_path / "nested" / "report.html", rows, blocks)
    page = path.read_text(encoding="utf-8")
    assert "const POLL_MS = 0;" in page  # frozen: no refresh
    assert "Plotly.react" in page
    assert path.stat().st_size > 3_000_000  # the bundle really is embedded
    assert "WETH/USDC" in page
    # Both directions ship, so the flip toggle works with no server behind it.
    assert "const EMBEDDED_INVERTED = null;" not in page
    assert "WETH per USDC" in page
    # Nothing the document loads comes from off-machine: no external script or
    # stylesheet references at all. (The Plotly bundle mentions cdn.plot.ly as a
    # default topojson config value; scatter/bar charts never request it.)
    assert "<script src=" not in page
    assert "<link " not in page
