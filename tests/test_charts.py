"""Figure construction, including what a hostile token symbol may reach.

Rows come from a real snapshot over the AMM simulator, so the figures are built
from the same column set production emits.
"""

from __future__ import annotations

import json

import httpx
import pandas as pd
import pytest

pytest.importorskip("plotly")

import amm_sim
from price_of_ethereum import FyndClient, SnapshotConfig, TokenMeta, collect_snapshot
from price_of_ethereum.charts import (
    book_map_figure,
    cost_curve_figure,
    figure_text,
    history_mid_figure,
    spread_curve_figure,
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


def test_axes_carry_the_real_token_symbols(measured) -> None:
    rows, blocks = measured
    robust_mid = blocks.iloc[0]["robust_mid"]
    figure = book_map_figure(rows, robust_mid, token_symbol="WETH", numeraire_symbol="USDC")
    assert figure.layout.yaxis.title.text == "execution price (USDC per WETH)"
    assert figure.layout.xaxis.title.text == "size (USDC)"
    assert figure.data[0].name == "ask (buy WETH)"


def test_book_map_marks_the_mid_and_the_band(measured) -> None:
    rows, blocks = measured
    robust_mid = blocks.iloc[0]["robust_mid"]
    figure = book_map_figure(rows, robust_mid)
    mid_lines = [shape for shape in figure.layout.shapes if shape.type == "line"]
    assert mid_lines and mid_lines[0].y0 == pytest.approx(robust_mid)
    bands = [shape for shape in figure.layout.shapes if shape.type == "rect"]
    assert bands and bands[0].x0 == pytest.approx(ROBUST_MID_MIN_DEPTH)


def test_book_map_shades_the_band_the_run_actually_used() -> None:
    # The band is in numeraire units. A pair whose numeraire is worth $600 votes
    # its mid from ~4-17 of them, and shading the dollar-shaped default instead
    # would claim the mid came from a notional 600x larger than it did.
    rows = pd.DataFrame(
        {
            "kind": ["curve", "curve"],
            "side": ["buy", "sell"],
            "size_numeraire": [4.5, 4.5],
            "execution_price": [158.0, 157.0],
            "impact_pct": [0.1, 0.1],
        }
    )
    figure = book_map_figure(rows, 157.5, band_min=4.167, band_max=16.667)
    bands = [shape for shape in figure.layout.shapes if shape.type == "rect"]
    assert bands
    assert bands[0].x0 == pytest.approx(4.167)
    assert bands[0].x1 == pytest.approx(16.667)


def test_spread_curve_empty_without_a_mid(measured) -> None:
    rows, _ = measured
    assert spread_curve_figure(rows, None).data == ()


HOSTILE_CLOSE_TAG = "</script><img src=x onerror=1>"
# "<!--" followed by "<script" puts the HTML tokenizer into "script data
# double escaped" state, where a later "</script>" no longer closes the
# element — a classic script-context smuggling trick, distinct from the
# plain closing-tag case above.
HOSTILE_COMMENT_ESCAPE = "<!--<script>alert(1)</script>"


HOSTILE_PLOTLY_MARKUP = '<a href="https://evil.example/phish">USDC</a>'


def caller_supplied_text(figure) -> list[str]:
    """Every string in a figure that a caller's symbol can reach.

    Read from the decoded figure rather than its JSON: the builders put their
    own `<br>` in hovertemplates, so searching the encoded blob for an angle
    bracket finds Plotly's markup instead of the caller's.
    """
    decoded = json.loads(figure.to_json())
    layout = decoded.get("layout", {})
    texts = [
        layout.get("title", {}).get("text", ""),
        layout.get("xaxis", {}).get("title", {}).get("text", ""),
        layout.get("yaxis", {}).get("title", {}).get("text", ""),
    ]
    texts += [trace.get("name", "") for trace in decoded.get("data", [])]
    return [text for text in texts if text]


def assert_figure_carries_no_markup(figure, payload: str) -> None:
    """Nothing a caller supplied is drawn as markup, and its text still shows."""
    rendered = caller_supplied_text(figure)
    for text in rendered:
        assert "<" not in text and ">" not in text, text
    stripped = figure_text(payload)
    assert any(stripped in text for text in rendered), rendered


@pytest.mark.parametrize(
    "payload", [HOSTILE_PLOTLY_MARKUP, HOSTILE_CLOSE_TAG, HOSTILE_COMMENT_ESCAPE]
)
def test_a_hostile_symbol_reaches_no_figure_as_markup(payload: str, measured) -> None:
    # A token symbol is on-chain metadata: anyone can mint one that reads as an
    # anchor tag. Every builder that renders a symbol strips it, so no caller
    # has to remember to.
    rows, blocks = measured
    for figure in (
        cost_curve_figure(rows, numeraire_symbol=payload),
        book_map_figure(rows, 2500.0, token_symbol=payload, numeraire_symbol=payload),
        spread_curve_figure(rows, 2500.0, numeraire_symbol=payload),
        history_mid_figure(blocks, token_symbol=payload, numeraire_symbol=payload),
    ):
        assert_figure_carries_no_markup(figure, payload)


def test_an_ordinary_symbol_is_left_alone() -> None:
    assert figure_text("USDC") == "USDC"
    assert figure_text("WETH.e") == "WETH.e"
