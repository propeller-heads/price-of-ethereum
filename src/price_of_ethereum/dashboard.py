"""Analytical dashboard over collected measurements.

Figures are built in Python from the JSONL the collector writes, serialized as
Plotly figure specs, and drawn by `Plotly.react` in the page. Nothing is fetched
from a CDN: the JS bundle ships inside the `plotly` package (the `viz` extra),
so a dashboard works offline and depends on no host but your own.

Two entry points share one page template — `serve.py` fetches the payload over
HTTP and refreshes it, `write_report` embeds one frozen payload plus the JS.
Every axis carries the real token symbols as its unit, and the whole view can be
flipped to the other side of the pair (`PriceView`) — prices invert and sizes are
restated in the other token at the mid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd

try:
    import plotly.graph_objects as go
    import plotly.io as plotly_io
    from plotly.offline import get_plotlyjs
except ImportError as error:  # pragma: no cover - exercised by the CLI guard
    raise ImportError(
        "the dashboard needs the viz extra: pip install 'price-of-ethereum[viz]' "
        "(or: uv sync --extra viz)"
    ) from error

from price_of_ethereum.pricing import ROBUST_MID_MAX_DEPTH, ROBUST_MID_MIN_DEPTH

# Which tick formatting an x axis needs; see `_base_layout`.
XAxisKind = Literal["linear", "size", "block"]

# Colorblind-safe pair; buy/ask warm, sell/bid cool, held consistent everywhere.
BUY_COLOR = "#d1610b"
SELL_COLOR = "#1f6fb4"
NEUTRAL_COLOR = "#7a7a7a"
FLAG_COLOR = "#a8324a"

LEVEL_TABLE_COLUMNS = (
    "side",
    "target_impact_pct",
    "size_numeraire",
    "impact_pct",
    "price_impact_bps",
    "execution_price",
    "bound",
    "target_reached",
    "derived_from",
    "n_pools",
    "protocols",
    "route_hash",
)

# Stored column -> display column, where the dashboard shows a different unit.
LEVEL_COLUMN_LABELS = {"price_impact_bps": "price_impact_pct"}


@dataclass(frozen=True)
class PriceView:
    """Which way round the market is presented, and what to call the units.

    Measurements are always numeraire per token (USDC per WETH) off a grid of
    numeraire notionals. `inverted` re-denominates the whole presentation to the
    other side of the pair — prices become token per numeraire and sizes are
    restated in token units at `mid`. Nothing stored changes; this is a display
    convention, not a re-measurement.

    Restating sizes needs a mid to divide by. Without one, sizes stay in the
    numeraire and `size_symbol` says so rather than mislabelling them.
    """

    token_symbol: str = "token"
    numeraire_symbol: str = "numeraire"
    inverted: bool = False
    mid: float | None = None

    @property
    def base_symbol(self) -> str:
        """The symbol prices are quoted *in*."""
        return self.token_symbol if self.inverted else self.numeraire_symbol

    @property
    def quote_symbol(self) -> str:
        """The symbol prices are quoted *for*."""
        return self.numeraire_symbol if self.inverted else self.token_symbol

    @property
    def price_unit(self) -> str:
        return f"{self.base_symbol} per {self.quote_symbol}"

    @property
    def pair_label(self) -> str:
        return f"{self.quote_symbol}/{self.base_symbol}"

    @property
    def other_pair_label(self) -> str:
        """The pair as it reads in the opposite direction — what a flip offers."""
        return f"{self.base_symbol}/{self.quote_symbol}"

    @property
    def sizes_restated(self) -> bool:
        return self.inverted and bool(self.mid)

    @property
    def size_symbol(self) -> str:
        return self.token_symbol if self.sizes_restated else self.numeraire_symbol

    def convert(self, price: float | None) -> float | None:
        if price is None or not self.inverted:
            return price
        return 1.0 / price if price else None

    def convert_series(self, prices: pd.Series) -> pd.Series:
        return 1.0 / prices if self.inverted else prices

    def convert_size(self, sizes: pd.Series) -> pd.Series:
        """Numeraire notionals restated in token units when inverted."""
        if not self.sizes_restated:
            return sizes
        assert self.mid is not None  # guarded by sizes_restated
        return sizes / self.mid

    def convert_size_value(self, size: float | None) -> float | None:
        if size is None or not self.sizes_restated or self.mid is None:
            return size
        return size / self.mid


DEFAULT_VIEW = PriceView()


def view_from_header(header: dict[str, Any], *, inverted: bool = False) -> PriceView:
    return PriceView(
        token_symbol=header.get("token_symbol") or "token",
        numeraire_symbol=header.get("numeraire_symbol") or "numeraire",
        inverted=inverted,
        mid=header.get("robust_mid"),
    )


def _base_layout(
    title: str, x_title: str, y_title: str, *, x_kind: XAxisKind = "linear"
) -> dict[str, Any]:
    """Chrome-free layout; the page patches colors to match its theme.

    `x_kind` controls tick formatting, which Plotly gets wrong by default for
    both of the axes used here:
      "size"  — log decades with SI suffixes (1k, 10k, 1M). The default also
                labels the 2x and 5x minor ticks, rendering bare "2" and "5"
                beside full numbers as if the scale had changed.
      "block" — plain integers. The default abbreviates a block number to
                "25.62425M", which is unreadable as an identifier.
    """
    x_axis: dict[str, Any] = {"title": {"text": x_title}}
    if x_kind == "size":
        x_axis |= {"type": "log", "dtick": 1, "tickformat": "~s"}
    elif x_kind == "block":
        x_axis |= {"type": "linear", "tickformat": "d", "exponentformat": "none"}
    else:
        x_axis["type"] = "linear"
    return {
        "title": {"text": title},
        "xaxis": x_axis,
        "yaxis": {"title": {"text": y_title}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "margin": {"l": 72, "r": 24, "t": 48, "b": 48},
        "hovermode": "x unified",
        "legend": {"orientation": "h", "y": -0.2},
    }


def _side_frames(rows: pd.DataFrame, kind: str) -> dict[str, pd.DataFrame]:
    if rows.empty or "kind" not in rows.columns:
        return {}
    selected = rows[rows["kind"] == kind]
    return {
        side: selected[selected["side"] == side].sort_values("size_numeraire")
        for side in ("buy", "sell")
        if not selected[selected["side"] == side].empty
    }


def cost_curve_figure(rows: pd.DataFrame, view: PriceView = DEFAULT_VIEW) -> go.Figure:
    """Measured price impact against trade size, both sides, anchors marked.

    Impact is a ratio, so it reads the same in either price direction.
    """
    figure = go.Figure()
    for side, frame in _side_frames(rows, "curve").items():
        figure.add_trace(
            go.Scatter(
                x=view.convert_size(frame["size_numeraire"]),
                y=frame["impact_pct"],
                mode="lines+markers",
                name=f"{side} (sweep)",
                line={"color": BUY_COLOR if side == "buy" else SELL_COLOR, "width": 2},
                marker={"size": 4},
            )
        )
    for side, frame in _side_frames(rows, "anchor").items():
        figure.add_trace(
            go.Scatter(
                x=view.convert_size(frame["size_numeraire"]),
                y=frame["impact_pct"],
                mode="markers",
                name=f"{side} (anchor)",
                marker={
                    "size": 11,
                    "symbol": "diamond-open",
                    "line": {"width": 2},
                    "color": BUY_COLOR if side == "buy" else SELL_COLOR,
                },
                customdata=frame[["target_impact_pct", "derived_from"]]
                if "target_impact_pct" in frame.columns
                else None,
                hovertemplate="target %{customdata[0]}%<br>actual %{y:.4f}%<br>%{customdata[1]}",
            )
        )
    figure.update_layout(
        **_base_layout(
            "Cost curve — price impact vs trade size",
            f"size ({view.size_symbol})",
            "impact (%)",
            x_kind="size",
        )
    )
    return figure


def book_map_figure(
    rows: pd.DataFrame, robust_mid: float | None, view: PriceView = DEFAULT_VIEW
) -> go.Figure:
    """Effective execution price per side, with the robust-mid band shaded."""
    figure = go.Figure()
    for side, frame in _side_frames(rows, "curve").items():
        figure.add_trace(
            go.Scatter(
                x=view.convert_size(frame["size_numeraire"]),
                y=view.convert_series(frame["execution_price"]),
                mode="lines",
                name=f"ask (buy {view.token_symbol})"
                if side == "buy"
                else f"bid (sell {view.token_symbol})",
                line={"color": BUY_COLOR if side == "buy" else SELL_COLOR, "width": 2},
            )
        )
    figure.update_layout(
        **_base_layout(
            "Book map — effective price vs notional",
            f"size ({view.size_symbol})",
            f"execution price ({view.price_unit})",
            x_kind="size",
        )
    )
    # The band the robust mid is voted from, so it is visible which rungs count.
    figure.add_vrect(
        x0=view.convert_size_value(ROBUST_MID_MIN_DEPTH),
        x1=view.convert_size_value(ROBUST_MID_MAX_DEPTH),
        fillcolor=NEUTRAL_COLOR,
        opacity=0.12,
        line_width=0,
        annotation_text="robust-mid band",
        annotation_position="top left",
    )
    shown_mid = view.convert(robust_mid)
    if shown_mid is not None:
        figure.add_hline(
            y=shown_mid,
            line={"color": NEUTRAL_COLOR, "dash": "dash", "width": 1},
            annotation_text=f"robust mid {shown_mid:,.6g}",
        )
    return figure


def spread_curve_figure(
    rows: pd.DataFrame, robust_mid: float | None, view: PriceView = DEFAULT_VIEW
) -> go.Figure:
    """Ask-minus-bid at each matched notional, as a percent of the mid.

    Computed on the measured (numeraire per token) prices; a spread relative to
    the mid is direction-independent.
    """
    figure = go.Figure()
    sides = _side_frames(rows, "curve")
    if len(sides) == 2 and robust_mid:
        matched = sides["buy"].merge(sides["sell"], on="size_numeraire", suffixes=("_buy", "_sell"))
        if not matched.empty:
            spread_pct = (
                (matched["execution_price_buy"] - matched["execution_price_sell"])
                / robust_mid
                * 100.0
            )
            figure.add_trace(
                go.Scatter(
                    x=view.convert_size(matched["size_numeraire"]),
                    y=spread_pct,
                    mode="lines+markers",
                    name="ask - bid",
                    line={"color": NEUTRAL_COLOR, "width": 2},
                    marker={"size": 4},
                )
            )
    figure.update_layout(
        **_base_layout(
            "Round-trip spread vs notional",
            f"size ({view.size_symbol})",
            "spread (% of mid)",
            x_kind="size",
        )
    )
    return figure


def history_mid_figure(blocks: pd.DataFrame, view: PriceView = DEFAULT_VIEW) -> go.Figure:
    figure = go.Figure()
    if not blocks.empty:
        ordered = blocks.sort_values("block_number")
        for column, color, name in (
            ("robust_mid", SELL_COLOR, "robust mid"),
            ("spot", NEUTRAL_COLOR, "spot"),
        ):
            if column in ordered.columns:
                figure.add_trace(
                    go.Scatter(
                        x=ordered["block_number"],
                        y=view.convert_series(ordered[column]),
                        mode="lines",
                        name=name,
                        line={"color": color, "width": 2 if column == "robust_mid" else 1},
                    )
                )
    figure.update_layout(
        **_base_layout("Mid across blocks", "block", f"price ({view.price_unit})", x_kind="block")
    )
    return figure


def history_depth_figure(blocks: pd.DataFrame, view: PriceView = DEFAULT_VIEW) -> go.Figure:
    """Depth the mid was taken at, colored by how the mid was won."""
    figure = go.Figure()
    if not blocks.empty and "median_depth" in blocks.columns:
        ordered = blocks.sort_values("block_number")
        sources = ordered.get("mid_source", pd.Series(["sweep_band"] * len(ordered)))
        figure.add_trace(
            go.Scatter(
                x=ordered["block_number"],
                y=view.convert_size(ordered["median_depth"]),
                mode="markers",
                name="median depth",
                marker={
                    "size": 7,
                    "color": [
                        SELL_COLOR if source == "sweep_band" else FLAG_COLOR for source in sources
                    ],
                },
                customdata=list(sources),
                hovertemplate="block %{x}<br>depth %{y:,.2f}<br>%{customdata}",
            )
        )
    figure.update_layout(
        **_base_layout(
            "Mid depth across blocks",
            "block",
            f"depth ({view.size_symbol})",
            x_kind="block",
        )
    )
    return figure


def history_health_figure(blocks: pd.DataFrame) -> go.Figure:
    """Collection latency per block; mixed-block snapshots called out."""
    figure = go.Figure()
    if not blocks.empty and "duration_ms" in blocks.columns:
        ordered = blocks.sort_values("block_number")
        mixed = ordered.get("mixed_block", pd.Series([False] * len(ordered)))
        figure.add_trace(
            go.Bar(
                x=ordered["block_number"],
                y=ordered["duration_ms"],
                name="snapshot duration",
                marker={
                    "color": [FLAG_COLOR if flag else NEUTRAL_COLOR for flag in mixed],
                },
                customdata=[bool(flag) for flag in mixed],
                hovertemplate="block %{x}<br>%{y} ms<br>mixed_block %{customdata}",
            )
        )
    figure.update_layout(
        **_base_layout("Snapshot duration across blocks", "block", "duration (ms)", x_kind="block")
    )
    return figure


def _latest_block_summary(blocks: pd.DataFrame) -> dict[str, Any]:
    if blocks.empty:
        return {}
    latest = blocks.sort_values("block_number").iloc[-1]
    return {key: _jsonable(value) for key, value in latest.to_dict().items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _level_table(rows: pd.DataFrame, view: PriceView) -> list[dict[str, Any]]:
    if rows.empty or "kind" not in rows.columns:
        return []
    anchors = rows[rows["kind"] == "anchor"]
    if anchors.empty:
        return []
    columns = [column for column in LEVEL_TABLE_COLUMNS if column in anchors.columns]
    ordered = anchors.sort_values(
        ["target_impact_pct", "side"] if "target_impact_pct" in anchors.columns else ["side"]
    )
    table: list[dict[str, Any]] = []
    for record in ordered[columns].to_dict("records"):
        row: dict[str, Any] = {}
        for column in columns:
            value = _jsonable(record[column])
            if column == "execution_price":
                value = view.convert(value)
            elif column == "size_numeraire":
                value = view.convert_size_value(value)
            elif column == "price_impact_bps":
                # Stored rows keep bps — the reference method's unit, pinned by
                # the golden test. Percent is shown because every figure does.
                value = None if value is None else value / 100.0
            row[LEVEL_COLUMN_LABELS.get(column, column)] = value
        table.append(row)
    return table


def build_payload(
    rows: pd.DataFrame, blocks: pd.DataFrame, *, inverted: bool = False
) -> dict[str, Any]:
    """Everything the page draws: header scalars, figure specs, level table.

    `inverted` flips the price direction throughout (values, axis units, the
    mid line, and the level table) without re-reading anything.
    """
    summary = _latest_block_summary(blocks)
    view = view_from_header(summary, inverted=inverted)
    robust_mid = summary.get("robust_mid")
    figures = {
        "cost_curve": cost_curve_figure(rows, view),
        "book_map": book_map_figure(rows, robust_mid, view),
        "spread_curve": spread_curve_figure(rows, robust_mid, view),
        "history_mid": history_mid_figure(blocks, view),
        "history_depth": history_depth_figure(blocks, view),
        "history_health": history_health_figure(blocks),
    }
    header = dict(summary)
    for price_field in ("spot", "robust_mid"):
        if price_field in header:
            header[price_field] = view.convert(header[price_field])
    for size_field in ("median_depth", "search_min", "search_max"):
        if size_field in header:
            header[size_field] = view.convert_size_value(header[size_field])
    curve_rows = rows[rows["kind"] == "curve"] if "kind" in rows.columns else rows
    return {
        "header": header,
        # Round-tripped through Plotly's encoder so numpy/pandas values inside
        # traces become plain JSON types the stdlib can then serialize.
        "figures": {
            name: json.loads(plotly_io.to_json(figure)) for name, figure in figures.items()
        },
        "levels": _level_table(rows, view),
        "view": {
            "inverted": view.inverted,
            "price_unit": view.price_unit,
            "pair_label": view.pair_label,
            "other_pair_label": view.other_pair_label,
            "size_symbol": view.size_symbol,
            "numeraire_symbol": view.numeraire_symbol,
            "token_symbol": view.token_symbol,
        },
        "provenance": {
            "blocks_recorded": len(blocks),
            "rows_latest_block": len(rows),
            "curve_rungs": len(curve_rows),
            "mixed_blocks": int(blocks["mixed_block"].sum()) if "mixed_block" in blocks else 0,
            "degraded_mids": int((blocks["mid_source"] != "sweep_band").sum())
            if "mid_source" in blocks
            else 0,
        },
    }


PAGE_TEMPLATE = """<title>__TITLE__</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --panel: #f6f7f9; --border: #d9dce1;
    --text: #16181d; --muted: #5c6270; --accent: #1f6fb4; --flag: #a8324a;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --panel: #171a21; --border: #2a2f3a;
      --text: #e6e8ec; --muted: #9aa1ae; --accent: #5aa7e6; --flag: #e5738d;
    }
  }
  :root[data-theme="light"] {
    --bg: #ffffff; --panel: #f6f7f9; --border: #d9dce1;
    --text: #16181d; --muted: #5c6270; --accent: #1f6fb4; --flag: #a8324a;
  }
  :root[data-theme="dark"] {
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a;
    --text: #e6e8ec; --muted: #9aa1ae; --accent: #5aa7e6; --flag: #e5738d;
  }
  body {
    margin: 0; padding: 1.5rem 1.25rem 3rem;
    background: var(--bg); color: var(--text);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
  .sub { color: var(--muted); font-size: .875rem; margin: 0 0 1rem; }
  .controls {
    display: flex; align-items: center; gap: .75rem;
    flex-wrap: wrap; margin-bottom: 1.5rem;
  }
  button.toggle {
    font: inherit; font-size: .85rem; cursor: pointer;
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 7px;
    padding: .4rem .8rem;
  }
  button.toggle:hover { border-color: var(--accent); color: var(--accent); }
  .hint { color: var(--muted); font-size: .78rem; }
  .stats {
    display: grid; gap: .75rem; margin-bottom: 1.75rem;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  }
  .stat {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: .7rem .85rem;
  }
  .stat .k {
    font-size: .7rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--muted); display: block; margin-bottom: .2rem;
  }
  .stat .v {
    font-size: 1.1rem; font-weight: 600;
    font-variant-numeric: tabular-nums; word-break: break-all;
  }
  .stat .u { font-size: .7rem; color: var(--muted); font-weight: 400; }
  .flag .v { color: var(--flag); }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: .5rem; margin-bottom: 1.25rem;
  }
  .chart { width: 100%; height: 380px; }
  h2 { font-size: 1rem; margin: 1.75rem 0 .6rem; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: .82rem; }
  th, td {
    text-align: right; padding: .4rem .55rem;
    border-bottom: 1px solid var(--border); white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: .68rem; }
  td:first-child, th:first-child, td.txt, th.txt { text-align: left; }
  footer { color: var(--muted); font-size: .78rem; margin-top: 2rem; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; }
  #status { font-size: .78rem; color: var(--muted); }
</style>
<div class="wrap">
  <h1 id="title">Price of Ethereum — measured depth</h1>
  <p class="sub">
    Every value is a Fynd quote or a direct function of quotes. No oracles, no estimates.
    <span id="status"></span>
  </p>
  <div class="controls">
    <button class="toggle" id="invert" type="button">flip direction</button>
    <span class="hint" id="view-hint"></span>
  </div>
  <div class="stats" id="stats"></div>
  <div class="panel"><div class="chart" id="cost_curve"></div></div>
  <div class="panel"><div class="chart" id="book_map"></div></div>
  <div class="panel"><div class="chart" id="spread_curve"></div></div>
  <h2>Depth levels (anchored measurements)</h2>
  <div class="scroll"><table id="levels"></table></div>
  <h2>Across recorded blocks</h2>
  <div class="panel"><div class="chart" id="history_mid"></div></div>
  <div class="panel"><div class="chart" id="history_depth"></div></div>
  <div class="panel"><div class="chart" id="history_health"></div></div>
  <footer id="provenance"></footer>
</div>
__PLOTLY__
<script>
const POLL_MS = __POLL_MS__;
const EMBEDDED = __PAYLOAD__;
const EMBEDDED_INVERTED = __EMBEDDED_INVERTED__;

const HEADER_KEYS = [
  "pair", "chain_id", "block_number", "spot", "robust_mid", "median_depth",
  "mid_source", "mixed_block", "gas_price_wei", "samples_per_side", "duration_ms",
];

let inverted = false;
let current = EMBEDDED;

function unitsFor(view) {
  const size = (view && view.size_symbol) || "numeraire";
  const price = (view && view.price_unit) || "numeraire per token";
  return {
    spot: price, robust_mid: price, median_depth: size,
    duration_ms: "ms", gas_price_wei: "wei",
    search_min: size, search_max: size,
  };
}

function isDark() {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced) return forced === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function themePatch() {
  const dark = isDark();
  const text = dark ? "#e6e8ec" : "#16181d";
  const grid = dark ? "#2a2f3a" : "#e3e6ea";
  return {
    font: { color: text, family: "ui-sans-serif, system-ui, sans-serif", size: 12 },
    xaxis: { gridcolor: grid, zerolinecolor: grid, linecolor: grid },
    yaxis: { gridcolor: grid, zerolinecolor: grid, linecolor: grid },
  };
}

function merge(layout, patch) {
  const out = Object.assign({}, layout, { font: patch.font });
  out.xaxis = Object.assign({}, layout.xaxis || {}, patch.xaxis);
  out.yaxis = Object.assign({}, layout.yaxis || {}, patch.yaxis);
  return out;
}

function fmt(value) {
  if (value === null || value === undefined) return "\\u2014";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") {
    if (Number.isInteger(value)) return value.toLocaleString();
    return value.toLocaleString(undefined, { maximumSignificantDigits: 8 });
  }
  return String(value);
}

function renderStats(header, view) {
  const host = document.getElementById("stats");
  if (!header || Object.keys(header).length === 0) {
    host.innerHTML = '<div class="stat"><span class="k">waiting</span>' +
      '<span class="v">no blocks yet</span></div>';
    return;
  }
  const units = unitsFor(view);
  host.innerHTML = HEADER_KEYS.filter((key) => key in header).map((key) => {
    const flag = (key === "mixed_block" && header[key]) ||
      (key === "mid_source" && header[key] !== "sweep_band");
    const unit = units[key] ? '<span class="u"> ' + units[key] + "</span>" : "";
    return '<div class="stat' + (flag ? " flag" : "") + '"><span class="k">' +
      key.replace(/_/g, " ") + '</span><span class="v">' + fmt(header[key]) + unit +
      "</span></div>";
  }).join("");
  const pair = (view && view.pair_label) || header.pair || "Price of Ethereum";
  document.getElementById("title").textContent = pair + " — measured depth";
}

function renderLevels(levels) {
  const table = document.getElementById("levels");
  if (!levels || levels.length === 0) {
    table.innerHTML = "<tr><td class='txt'>no anchored levels in this block</td></tr>";
    return;
  }
  const columns = Object.keys(levels[0]);
  const textual = new Set(["side", "bound", "derived_from", "protocols", "route_hash"]);
  const head = "<tr>" + columns.map((column) =>
    '<th class="' + (textual.has(column) ? "txt" : "") + '">' +
    column.replace(/_/g, " ") + "</th>").join("") + "</tr>";
  const body = levels.map((row) => "<tr>" + columns.map((column) =>
    '<td class="' + (textual.has(column) ? "txt" : "") + '">' +
    fmt(row[column]) + "</td>").join("") + "</tr>").join("");
  table.innerHTML = head + body;
}

function renderProvenance(provenance, header, view) {
  const parts = Object.entries(provenance || {}).map(
    ([key, value]) => key.replace(/_/g, " ") + ": " + fmt(value));
  if (header && header.search_min !== undefined) {
    const numeraire = (view && view.numeraire_symbol) || "numeraire";
    parts.push("search range: " + fmt(header.search_min) + " to " +
      fmt(header.search_max) + " " + numeraire);
  }
  document.getElementById("provenance").textContent = parts.join("  \\u00b7  ");
}

function renderViewHint(view) {
  const button = document.getElementById("invert");
  const hint = document.getElementById("view-hint");
  if (!view) { button.disabled = true; return; }
  button.disabled = false;
  // The button names the direction a click switches TO, not the current one.
  button.textContent = "show " + view.other_pair_label;
  hint.textContent = "showing " + view.pair_label + ": prices in " +
    view.price_unit + ", sizes in " + view.size_symbol;
}

function draw(payload) {
  const patch = themePatch();
  const config = { displaylogo: false, responsive: true };
  Object.entries(payload.figures || {}).forEach(([name, figure]) => {
    const host = document.getElementById(name);
    if (host) Plotly.react(host, figure.data, merge(figure.layout, patch), config);
  });
  renderStats(payload.header, payload.view);
  renderLevels(payload.levels);
  renderProvenance(payload.provenance, payload.header, payload.view);
  renderViewHint(payload.view);
}

async function refresh() {
  try {
    const response = await fetch("data.json?invert=" + (inverted ? "1" : "0"),
      { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    current = await response.json();
    draw(current);
    document.getElementById("status").textContent =
      "\\u00b7 updated " + new Date().toLocaleTimeString();
  } catch (error) {
    document.getElementById("status").textContent =
      "\\u00b7 refresh failed: " + error.message;
  }
}

document.getElementById("invert").addEventListener("click", () => {
  inverted = !inverted;
  if (POLL_MS > 0) {
    refresh();
  } else if (EMBEDDED_INVERTED) {
    // Frozen report: both directions are embedded, so flipping needs no server.
    current = inverted ? EMBEDDED_INVERTED : EMBEDDED;
    draw(current);
  }
});

if (current) {
  inverted = Boolean(current.view && current.view.inverted);
  draw(current);
}

if (POLL_MS > 0) {
  refresh();
  setInterval(refresh, POLL_MS);
}

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (current) draw(current);
});
new MutationObserver(() => { if (current) draw(current); }).observe(
  document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
</script>
"""


def render_page(
    *,
    title: str,
    poll_ms: int,
    payload: dict[str, Any] | None,
    inline_js: bool,
    inverted_payload: dict[str, Any] | None = None,
) -> str:
    """The dashboard page. `poll_ms=0` disables refresh (frozen report), in which
    case `inverted_payload` supplies the flipped direction so the toggle still
    works with no server behind it."""
    plotly_tag = (
        f"<script>{get_plotlyjs()}</script>" if inline_js else '<script src="plotly.js"></script>'
    )
    return (
        PAGE_TEMPLATE.replace("__TITLE__", title)
        .replace("__PLOTLY__", plotly_tag)
        .replace("__POLL_MS__", str(poll_ms))
        .replace("__PAYLOAD__", json.dumps(payload) if payload is not None else "null")
        .replace(
            "__EMBEDDED_INVERTED__",
            json.dumps(inverted_payload) if inverted_payload is not None else "null",
        )
    )


def write_report(
    path: Path | str, rows: pd.DataFrame, blocks: pd.DataFrame, *, title: str | None = None
) -> Path:
    """Write a frozen, fully self-contained HTML report (JS and both price
    directions embedded, so the flip toggle works offline)."""
    payload = build_payload(rows, blocks)
    inverted_payload = build_payload(rows, blocks, inverted=True)
    pair = payload["view"]["pair_label"]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_page(
            title=title or f"{pair} — measured depth",
            poll_ms=0,
            payload=payload,
            inline_js=True,
            inverted_payload=inverted_payload,
        ),
        encoding="utf-8",
    )
    return path
