"""Analytical dashboard over collected measurements.

Figures are built in Python from the JSONL the collector writes, serialized as
Plotly figure specs, and drawn by `Plotly.react` in the page. Nothing is fetched
from a CDN: the JS bundle ships inside the `plotly` package (the `viz` extra),
so a dashboard works offline and depends on no host but your own.

Two entry points share one page template — `serve.py` fetches the payload over
HTTP and refreshes it, `write_report` embeds one frozen payload plus the JS.
Every panel keeps one unit per axis; mid/spot are numeraire per token, depths and
notionals are numeraire, spreads are basis points, durations milliseconds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def _base_layout(title: str, x_title: str, y_title: str, *, log_x: bool = False) -> dict[str, Any]:
    """Chrome-free layout; the page patches colors to match its theme."""
    return {
        "title": {"text": title},
        "xaxis": {"title": {"text": x_title}, "type": "log" if log_x else "linear"},
        "yaxis": {"title": {"text": y_title}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "margin": {"l": 64, "r": 24, "t": 48, "b": 48},
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


def cost_curve_figure(rows: pd.DataFrame) -> go.Figure:
    """Measured price impact against trade size, both sides, anchors marked."""
    figure = go.Figure()
    for side, frame in _side_frames(rows, "curve").items():
        figure.add_trace(
            go.Scatter(
                x=frame["size_numeraire"],
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
                x=frame["size_numeraire"],
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
            "Cost curve — price impact vs trade size", "size (numeraire)", "impact (%)", log_x=True
        )
    )
    return figure


def book_map_figure(rows: pd.DataFrame, robust_mid: float | None) -> go.Figure:
    """Effective execution price per side, with the robust-mid band shaded."""
    figure = go.Figure()
    for side, frame in _side_frames(rows, "curve").items():
        figure.add_trace(
            go.Scatter(
                x=frame["size_numeraire"],
                y=frame["execution_price"],
                mode="lines",
                name="ask (buy token)" if side == "buy" else "bid (sell token)",
                line={"color": BUY_COLOR if side == "buy" else SELL_COLOR, "width": 2},
            )
        )
    figure.update_layout(
        **_base_layout(
            "Book map — effective price vs notional",
            "size (numeraire)",
            "execution price (numeraire per token)",
            log_x=True,
        )
    )
    # The band the robust mid is voted from, so it is visible which rungs count.
    figure.add_vrect(
        x0=ROBUST_MID_MIN_DEPTH,
        x1=ROBUST_MID_MAX_DEPTH,
        fillcolor=NEUTRAL_COLOR,
        opacity=0.12,
        line_width=0,
        annotation_text="robust-mid band",
        annotation_position="top left",
    )
    if robust_mid is not None:
        figure.add_hline(
            y=robust_mid,
            line={"color": NEUTRAL_COLOR, "dash": "dash", "width": 1},
            annotation_text=f"robust mid {robust_mid:,.6g}",
        )
    return figure


def spread_curve_figure(rows: pd.DataFrame, robust_mid: float | None) -> go.Figure:
    """Ask-minus-bid at each matched notional, in basis points of the mid."""
    figure = go.Figure()
    sides = _side_frames(rows, "curve")
    if len(sides) == 2 and robust_mid:
        matched = sides["buy"].merge(sides["sell"], on="size_numeraire", suffixes=("_buy", "_sell"))
        if not matched.empty:
            spread_bps = (
                (matched["execution_price_buy"] - matched["execution_price_sell"])
                / robust_mid
                * 10_000.0
            )
            figure.add_trace(
                go.Scatter(
                    x=matched["size_numeraire"],
                    y=spread_bps,
                    mode="lines+markers",
                    name="ask - bid",
                    line={"color": NEUTRAL_COLOR, "width": 2},
                    marker={"size": 4},
                )
            )
    figure.update_layout(
        **_base_layout(
            "Round-trip spread vs notional", "size (numeraire)", "spread (bps of mid)", log_x=True
        )
    )
    return figure


def history_mid_figure(blocks: pd.DataFrame) -> go.Figure:
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
                        y=ordered[column],
                        mode="lines",
                        name=name,
                        line={"color": color, "width": 2 if column == "robust_mid" else 1},
                    )
                )
    figure.update_layout(
        **_base_layout("Mid across blocks", "block", "price (numeraire per token)")
    )
    return figure


def history_depth_figure(blocks: pd.DataFrame) -> go.Figure:
    """Depth the mid was taken at, colored by how the mid was won."""
    figure = go.Figure()
    if not blocks.empty and "median_depth" in blocks.columns:
        ordered = blocks.sort_values("block_number")
        sources = ordered.get("mid_source", pd.Series(["sweep_band"] * len(ordered)))
        figure.add_trace(
            go.Scatter(
                x=ordered["block_number"],
                y=ordered["median_depth"],
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
    figure.update_layout(**_base_layout("Mid depth across blocks", "block", "depth (numeraire)"))
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
        **_base_layout("Snapshot duration across blocks", "block", "duration (ms)")
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


def _level_table(rows: pd.DataFrame) -> list[dict[str, Any]]:
    if rows.empty or "kind" not in rows.columns:
        return []
    anchors = rows[rows["kind"] == "anchor"]
    if anchors.empty:
        return []
    columns = [column for column in LEVEL_TABLE_COLUMNS if column in anchors.columns]
    ordered = anchors.sort_values(
        ["target_impact_pct", "side"] if "target_impact_pct" in anchors.columns else ["side"]
    )
    return [
        {column: _jsonable(record[column]) for column in columns}
        for record in ordered[columns].to_dict("records")
    ]


def build_payload(rows: pd.DataFrame, blocks: pd.DataFrame) -> dict[str, Any]:
    """Everything the page draws: header scalars, figure specs, level table."""
    summary = _latest_block_summary(blocks)
    robust_mid = summary.get("robust_mid")
    figures = {
        "cost_curve": cost_curve_figure(rows),
        "book_map": book_map_figure(rows, robust_mid),
        "spread_curve": spread_curve_figure(rows, robust_mid),
        "history_mid": history_mid_figure(blocks),
        "history_depth": history_depth_figure(blocks),
        "history_health": history_health_figure(blocks),
    }
    curve_rows = rows[rows["kind"] == "curve"] if "kind" in rows.columns else rows
    return {
        "header": summary,
        # Round-tripped through Plotly's encoder so numpy/pandas values inside
        # traces become plain JSON types the stdlib can then serialize.
        "figures": {
            name: json.loads(plotly_io.to_json(figure)) for name, figure in figures.items()
        },
        "levels": _level_table(rows),
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
  .sub { color: var(--muted); font-size: .875rem; margin: 0 0 1.5rem; }
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

const UNITS = {
  spot: "numeraire/token", robust_mid: "numeraire/token", median_depth: "numeraire",
  duration_ms: "ms", gas_price_wei: "wei", search_min: "numeraire", search_max: "numeraire",
};
const HEADER_KEYS = [
  "pair", "chain_id", "block_number", "spot", "robust_mid", "median_depth",
  "mid_source", "mixed_block", "gas_price_wei", "samples_per_side", "duration_ms",
];

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
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") {
    if (Number.isInteger(value)) return value.toLocaleString();
    return value.toLocaleString(undefined, { maximumSignificantDigits: 8 });
  }
  return String(value);
}

function renderStats(header) {
  const host = document.getElementById("stats");
  if (!header || Object.keys(header).length === 0) {
    host.innerHTML = '<div class="stat"><span class="k">waiting</span>' +
      '<span class="v">no blocks yet</span></div>';
    return;
  }
  host.innerHTML = HEADER_KEYS.filter((key) => key in header).map((key) => {
    const flag = (key === "mixed_block" && header[key]) ||
      (key === "mid_source" && header[key] !== "sweep_band");
    const unit = UNITS[key] ? '<span class="u"> ' + UNITS[key] + "</span>" : "";
    return '<div class="stat' + (flag ? " flag" : "") + '"><span class="k">' +
      key.replace(/_/g, " ") + '</span><span class="v">' + fmt(header[key]) + unit +
      "</span></div>";
  }).join("");
  document.getElementById("title").textContent =
    (header.pair || "Price of Ethereum") + " — measured depth";
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

function renderProvenance(provenance, header) {
  const parts = Object.entries(provenance || {}).map(
    ([key, value]) => key.replace(/_/g, " ") + ": " + fmt(value));
  if (header && header.search_min !== undefined) {
    parts.push("search range: " + fmt(header.search_min) + " to " + fmt(header.search_max) +
      " numeraire");
  }
  document.getElementById("provenance").textContent = parts.join("  ·  ");
}

function draw(payload) {
  const patch = themePatch();
  const config = { displaylogo: false, responsive: true };
  Object.entries(payload.figures || {}).forEach(([name, figure]) => {
    const host = document.getElementById(name);
    if (host) Plotly.react(host, figure.data, merge(figure.layout, patch), config);
  });
  renderStats(payload.header);
  renderLevels(payload.levels);
  renderProvenance(payload.provenance, payload.header);
}

let current = EMBEDDED;
if (current) draw(current);

async function refresh() {
  try {
    const response = await fetch("data.json", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    current = await response.json();
    draw(current);
    document.getElementById("status").textContent =
      "· updated " + new Date().toLocaleTimeString();
  } catch (error) {
    document.getElementById("status").textContent = "· refresh failed: " + error.message;
  }
}

if (POLL_MS > 0) {
  refresh();
  setInterval(refresh, POLL_MS);
}
["change", ""].forEach(() => {});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (current) draw(current);
});
new MutationObserver(() => { if (current) draw(current); }).observe(
  document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
</script>
"""


def render_page(
    *, title: str, poll_ms: int, payload: dict[str, Any] | None, inline_js: bool
) -> str:
    """The dashboard page. `poll_ms=0` disables refresh (frozen report)."""
    plotly_tag = (
        f"<script>{get_plotlyjs()}</script>" if inline_js else '<script src="plotly.js"></script>'
    )
    return (
        PAGE_TEMPLATE.replace("__TITLE__", title)
        .replace("__PLOTLY__", plotly_tag)
        .replace("__POLL_MS__", str(poll_ms))
        .replace("__PAYLOAD__", json.dumps(payload) if payload is not None else "null")
    )


def write_report(
    path: Path | str, rows: pd.DataFrame, blocks: pd.DataFrame, *, title: str | None = None
) -> Path:
    """Write a frozen, fully self-contained HTML report (JS and data embedded)."""
    payload = build_payload(rows, blocks)
    pair = payload["header"].get("pair") or "measured depth"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_page(
            title=title or f"{pair} — measured depth",
            poll_ms=0,
            payload=payload,
            inline_js=True,
        ),
        encoding="utf-8",
    )
    return path
