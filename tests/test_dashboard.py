"""Dashboard tests: figure construction and the static HTML report.

Rows come from a real snapshot over the AMM simulator, so the figures are built
from the same column set production emits.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

pytest.importorskip("plotly")

import amm_sim
from price_of_ethereum import FyndClient, SnapshotConfig, TokenMeta, collect_snapshot, dashboard
from price_of_ethereum.dashboard import (
    book_map_figure,
    cost_curve_figure,
    history_mid_figure,
    level_table_figure,
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


def test_spread_curve_empty_without_a_mid(measured) -> None:
    rows, _ = measured
    assert spread_curve_figure(rows, None).data == ()


def test_level_table_reports_impact_in_percent(measured) -> None:
    rows, _ = measured
    table = level_table_figure(rows).data[0]
    headers = list(table.header.values)
    assert "price impact bps" not in headers
    assert headers.index("price impact pct") == headers.index("impact pct") + 1
    anchors = rows[rows["kind"] == "anchor"]
    stored_bps = anchors.sort_values(["target_impact_pct", "side"]).iloc[0]["price_impact_bps"]
    shown = table.cells.values[headers.index("price impact pct")][0]
    assert float(shown) == pytest.approx(stored_bps / 100.0, rel=1e-5)


def test_level_table_lists_one_row_per_side(measured) -> None:
    rows, _ = measured
    table = level_table_figure(rows).data[0]
    side_column = table.cells.values[list(table.header.values).index("side")]
    assert sorted(side_column) == ["buy", "sell"]  # one per side at the single anchor target


def test_level_table_tolerates_no_anchors() -> None:
    table = level_table_figure(pd.DataFrame()).data[0]
    assert list(table.header.values) == []
    assert list(table.cells.values) == []


def test_report_is_self_contained(tmp_path: Path, measured) -> None:
    rows, blocks = measured
    path = write_report(tmp_path / "nested" / "report.html", rows, blocks)
    page = path.read_text(encoding="utf-8")
    assert path.stat().st_size > 3_000_000  # the bundle really is embedded
    assert "WETH/USDC" in page
    assert "execution price (USDC per WETH)" in page
    # Nothing the document loads comes from off-machine: no external script or
    # stylesheet references at all. (The Plotly bundle mentions cdn.plot.ly as a
    # default topojson config value; scatter/bar charts never request it.)
    assert "<script src=" not in page
    assert "<link " not in page


def test_report_draws_every_figure_and_inlines_the_bundle_once(tmp_path: Path, measured) -> None:
    rows, blocks = measured
    page = write_report(tmp_path / "report.html", rows, blocks).read_text(encoding="utf-8")
    assert page.count('class="plotly-graph-div"') == 7
    assert page.count("window.PlotlyConfig") == 1  # the bundle ships with the first figure only
    for title in (
        "Cost curve",
        "Book map",
        "Round-trip spread",
        "Depth levels",
        "Mid across blocks",
        "Mid depth across blocks",
        "Snapshot duration across blocks",
    ):
        assert title in page


def test_this_package_writes_no_javascript() -> None:
    # The report's only executable code is Plotly's bundle and the newPlot calls
    # Plotly itself emits. Nothing here is JS a Python test could never run, so
    # this greps the source that produces the page rather than the page.
    source = Path(dashboard.__file__).read_text(encoding="utf-8")
    for javascript in ("<script", "function(", "=>", "innerHTML", "addEventListener", "document."):
        assert javascript not in source


def test_report_script_tags_are_all_plotly(tmp_path: Path, measured) -> None:
    rows, blocks = measured
    page = write_report(tmp_path / "report.html", rows, blocks).read_text(encoding="utf-8")
    # One bundle, one PlotlyConfig preamble, one newPlot call per figure.
    assert page.count("<script") == 1 + 1 + page.count('class="plotly-graph-div"')


def test_report_tolerates_no_data(tmp_path: Path) -> None:
    path = write_report(tmp_path / "empty.html", pd.DataFrame(), pd.DataFrame())
    page = path.read_text(encoding="utf-8")
    assert "No block summaries recorded yet." in page
    assert "blocks recorded: 0" in page


HOSTILE_CLOSE_TAG = "</script><img src=x onerror=1>"
# "<!--" followed by "<script" puts the HTML tokenizer into "script data
# double escaped" state, where a later "</script>" no longer closes the
# element — a classic script-context smuggling trick, distinct from the
# plain closing-tag case above.
HOSTILE_COMMENT_ESCAPE = "<!--<script>alert(1)</script>"


@pytest.mark.parametrize("hostile", [HOSTILE_CLOSE_TAG, HOSTILE_COMMENT_ESCAPE])
def test_report_escapes_a_hostile_title(hostile: str, tmp_path: Path, measured) -> None:
    rows, blocks = measured
    path = write_report(tmp_path / "hostile.html", rows, blocks, title=f"ETH/USDC{hostile}")
    assert hostile not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("hostile", [HOSTILE_CLOSE_TAG, HOSTILE_COMMENT_ESCAPE])
def test_report_escapes_hostile_protocol_names(hostile: str, tmp_path: Path, measured) -> None:
    # protocols is third-party data (from Fynd), so a hostile name here is the
    # realistic case for a report that gets shared around.
    rows, blocks = measured
    rows = rows.copy()
    for index in rows.index[rows["kind"] == "anchor"]:
        rows.at[index, "protocols"] = [hostile]
    page = write_report(tmp_path / "hostile.html", rows, blocks).read_text(encoding="utf-8")
    assert hostile not in page
    assert "\\u003c" in page  # Plotly's serializer escapes every "<" it embeds


@pytest.mark.parametrize("hostile", [HOSTILE_CLOSE_TAG, HOSTILE_COMMENT_ESCAPE])
def test_report_escapes_a_hostile_token_symbol(hostile: str, tmp_path: Path, measured) -> None:
    # Token symbols reach the page twice: the block-summary stats and the axis
    # labels Plotly serializes.
    rows, blocks = measured
    blocks = blocks.copy()
    blocks.loc[:, "token_symbol"] = hostile
    page = write_report(tmp_path / "hostile.html", rows, blocks).read_text(encoding="utf-8")
    assert hostile not in page


HOSTILE_PLOTLY_MARKUP = '<a href="https://evil.example/phish">USDC</a>'


def assert_payload_carries_no_markup(page: str) -> None:
    """The anchor tag survives nowhere, raw or JSON-escaped.

    Checking for the anchor specifically rather than for any angle bracket:
    figures carry their own `<br>` in hovertemplates, and Plotly's bundle ships
    an ESRI map attribution containing a real anchor tag, so a broader search
    would match either of those instead of the payload.
    """
    assert HOSTILE_PLOTLY_MARKUP not in page
    assert "\\u003ca" not in page.lower()  # an escaped <a ...> Plotly would render
    assert "evil.example" in page  # the text itself survives, stripped of markup


@pytest.mark.parametrize("field", ["token_symbol", "numeraire_symbol"])
def test_report_strips_plotly_markup_from_token_symbols(
    field: str, tmp_path: Path, measured
) -> None:
    # An ERC-20 names itself, so a symbol is attacker-chosen. Plotly renders its
    # own pseudo-HTML inside figure text, which JSON escaping does not stop --
    # the browser decodes the string before Plotly draws it. A live link here
    # would ride along in a report meant to be shared.
    rows, blocks = measured
    blocks = blocks.copy()
    blocks.loc[:, field] = HOSTILE_PLOTLY_MARKUP
    page = write_report(tmp_path / "markup.html", rows, blocks).read_text(encoding="utf-8")
    assert_payload_carries_no_markup(page)


def test_report_strips_plotly_markup_from_protocol_names(tmp_path: Path, measured) -> None:
    rows, blocks = measured
    rows = rows.copy()
    for index in rows.index[rows["kind"] == "anchor"]:
        rows.at[index, "protocols"] = [HOSTILE_PLOTLY_MARKUP]
    page = write_report(tmp_path / "markup.html", rows, blocks).read_text(encoding="utf-8")
    assert_payload_carries_no_markup(page)


def test_figure_text_keeps_ordinary_symbols_intact() -> None:
    # Stripping must not mangle the symbols anyone actually measures.
    for symbol in ("WETH", "USDC", "wstETH", "1INCH", "aUSDC.e"):
        assert dashboard.figure_text(symbol) == symbol
