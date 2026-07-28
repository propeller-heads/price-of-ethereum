"""Analytical figures over collected measurements, and the static HTML report.

Figures are built in Python from the JSONL the collector writes and rendered by
Plotly itself — `plotly.io.to_html` emits each figure's div and the bundle is
inlined once, so a report opens from a file with no server and no network. Axes
carry the real token symbols recorded in each block summary, and prices read
numeraire per token throughout.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Literal

import pandas as pd

try:
    import plotly.graph_objects as go
    import plotly.io as plotly_io
except ImportError as error:  # pragma: no cover - exercised by the CLI guard
    raise ImportError(
        "the report needs the viz extra: pip install 'price-of-ethereum[viz]' "
        "(or: uv sync --extra viz)"
    ) from error

from price_of_ethereum.pricing import ROBUST_MID_MAX_DEPTH, ROBUST_MID_MIN_DEPTH

# Which tick formatting an x axis needs; see `_base_layout`.
XAxisKind = Literal["linear", "size", "block"]

# Plotly renders a small pseudo-HTML dialect inside figure text — <a href>,
# <span style>, <b> and friends — which JSON escaping does not neutralise,
# because the browser decodes the string back before Plotly draws it. An ERC-20
# picks its own symbol and Fynd names its own protocols, so both are untrusted
# and could otherwise put a live link into a report meant to be shared.
FIGURE_MARKUP = re.compile(r"[<>]|[\x00-\x1f\x7f-\x9f]")


def figure_text(value: str) -> str:
    """Strip anything Plotly would treat as markup from a third-party string.

    Display only — the recorded JSONL keeps every symbol and protocol name
    exactly as the upstream reported it.
    """
    return FIGURE_MARKUP.sub("", value)


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

# Stored column -> display column, where the table shows a different unit.
LEVEL_COLUMN_LABELS = {"price_impact_bps": "price_impact_pct"}

# Block-summary scalars worth showing above the charts, in reading order.
SUMMARY_FIELDS = (
    "pair",
    "chain_id",
    "block_number",
    "spot",
    "robust_mid",
    "median_depth",
    "mid_source",
    "mixed_block",
    "gas_price_wei",
    "samples_per_side",
    "duration_ms",
)

PLOTLY_CONFIG = {"displaylogo": False, "responsive": True}

REPORT_CSS = """
  body {
    margin: 0; padding: 1.5rem 1.25rem 3rem; background: #ffffff; color: #16181d;
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
  .sub { color: #5c6270; font-size: .875rem; margin: 0 0 1.5rem; }
  .stats {
    display: grid; gap: .75rem; margin-bottom: 1.75rem;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  }
  .stat {
    background: #f6f7f9; border: 1px solid #d9dce1;
    border-radius: 8px; padding: .7rem .85rem;
  }
  .stat .k {
    font-size: .7rem; text-transform: uppercase; letter-spacing: .06em;
    color: #5c6270; display: block; margin-bottom: .2rem;
  }
  .stat .v {
    font-size: 1.1rem; font-weight: 600;
    font-variant-numeric: tabular-nums; word-break: break-all;
  }
  .panel {
    background: #f6f7f9; border: 1px solid #d9dce1;
    border-radius: 10px; padding: .5rem; margin-bottom: 1.25rem;
  }
  footer { color: #5c6270; font-size: .78rem; margin-top: 2rem; }
"""


def _base_layout(
    title: str, x_title: str, y_title: str, *, x_kind: XAxisKind = "linear"
) -> dict[str, Any]:
    """Shared figure chrome.

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
        "template": "plotly_white",
        "title": {"text": title},
        "xaxis": x_axis,
        "yaxis": {"title": {"text": y_title}},
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


def cost_curve_figure(rows: pd.DataFrame, *, numeraire_symbol: str = "numeraire") -> go.Figure:
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
            "Cost curve — price impact vs trade size",
            f"size ({numeraire_symbol})",
            "impact (%)",
            x_kind="size",
        )
    )
    return figure


def book_map_figure(
    rows: pd.DataFrame,
    robust_mid: float | None,
    *,
    token_symbol: str = "token",
    numeraire_symbol: str = "numeraire",
) -> go.Figure:
    """Effective execution price per side, with the robust-mid band shaded."""
    figure = go.Figure()
    for side, frame in _side_frames(rows, "curve").items():
        figure.add_trace(
            go.Scatter(
                x=frame["size_numeraire"],
                y=frame["execution_price"],
                mode="lines",
                name=f"ask (buy {token_symbol})" if side == "buy" else f"bid (sell {token_symbol})",
                line={"color": BUY_COLOR if side == "buy" else SELL_COLOR, "width": 2},
            )
        )
    figure.update_layout(
        **_base_layout(
            "Book map — effective price vs notional",
            f"size ({numeraire_symbol})",
            f"execution price ({numeraire_symbol} per {token_symbol})",
            x_kind="size",
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


def spread_curve_figure(
    rows: pd.DataFrame, robust_mid: float | None, *, numeraire_symbol: str = "numeraire"
) -> go.Figure:
    """Ask-minus-bid at each matched notional, as a percent of the mid."""
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
                    x=matched["size_numeraire"],
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
            f"size ({numeraire_symbol})",
            "spread (% of mid)",
            x_kind="size",
        )
    )
    return figure


def history_mid_figure(
    blocks: pd.DataFrame, *, token_symbol: str = "token", numeraire_symbol: str = "numeraire"
) -> go.Figure:
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
        **_base_layout(
            "Mid across blocks",
            "block",
            f"price ({numeraire_symbol} per {token_symbol})",
            x_kind="block",
        )
    )
    return figure


def history_depth_figure(blocks: pd.DataFrame, *, numeraire_symbol: str = "numeraire") -> go.Figure:
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
    figure.update_layout(
        **_base_layout(
            "Mid depth across blocks",
            "block",
            f"depth ({numeraire_symbol})",
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


def _cell_text(value: Any) -> str:
    """One table cell as text: sequences joined, missing values dashed."""
    if not isinstance(value, str) and hasattr(value, "__len__"):
        return figure_text(", ".join(str(item) for item in value))
    if value is None or pd.isna(value):
        return "—"
    if isinstance(value, float):
        return f"{value:,.6g}"
    return figure_text(str(value))


def level_table_figure(rows: pd.DataFrame) -> go.Figure:
    """The anchored measurements, as a Plotly table.

    Rows store price impact in bps; the table shows percent so it reads in the
    same unit as every chart.
    """
    anchors = rows[rows["kind"] == "anchor"] if "kind" in rows.columns else rows.iloc[:0]
    columns = [column for column in LEVEL_TABLE_COLUMNS if column in anchors.columns]
    sort_columns = ["target_impact_pct", "side"] if "target_impact_pct" in columns else columns[:1]
    ordered = anchors.sort_values(sort_columns) if sort_columns else anchors
    cells = []
    for column in columns:
        values = ordered[column] / 100.0 if column == "price_impact_bps" else ordered[column]
        cells.append([_cell_text(value) for value in values])
    figure = go.Figure(
        go.Table(
            header={
                "values": [
                    LEVEL_COLUMN_LABELS.get(column, column).replace("_", " ") for column in columns
                ],
                "align": "left",
                "fill": {"color": "#eef0f3"},
            },
            cells={"values": cells, "align": "left", "height": 24},
        )
    )
    figure.update_layout(
        template="plotly_white",
        title={"text": "Depth levels — anchored measurements"},
        margin={"l": 24, "r": 24, "t": 48, "b": 24},
    )
    return figure


def _latest_block(blocks: pd.DataFrame) -> pd.Series | None:
    if blocks.empty or "block_number" not in blocks.columns:
        return None
    return blocks.sort_values("block_number").iloc[-1]


def _symbol(latest: pd.Series | None, field: str, fallback: str) -> str:
    if latest is None or field not in latest:
        return fallback
    value = latest[field]
    if not isinstance(value, str):
        return fallback
    return figure_text(value) or fallback


def _robust_mid(latest: pd.Series | None) -> float | None:
    if latest is None or "robust_mid" not in latest or pd.isna(latest["robust_mid"]):
        return None
    return float(latest["robust_mid"])


def _summary_html(latest: pd.Series | None) -> str:
    if latest is None:
        return '<p class="sub">No block summaries recorded yet.</p>'
    stats = [
        f'<div class="stat"><span class="k">{html.escape(field.replace("_", " "))}</span>'
        f'<span class="v">{html.escape(_cell_text(latest[field]))}</span></div>'
        for field in SUMMARY_FIELDS
        if field in latest
    ]
    return f'<div class="stats">{"".join(stats)}</div>'


def _provenance_text(rows: pd.DataFrame, blocks: pd.DataFrame) -> str:
    curve_rows = rows[rows["kind"] == "curve"] if "kind" in rows.columns else rows
    return "  ·  ".join(
        (
            f"blocks recorded: {len(blocks)}",
            f"rows in latest block: {len(rows)}",
            f"curve rungs: {len(curve_rows)}",
            f"mixed blocks: {int(blocks['mixed_block'].sum()) if 'mixed_block' in blocks else 0}",
            "degraded mids: "
            f"{int((blocks['mid_source'] != 'sweep_band').sum()) if 'mid_source' in blocks else 0}",
        )
    )


def write_report(
    path: Path | str, rows: pd.DataFrame, blocks: pd.DataFrame, *, title: str | None = None
) -> Path:
    """Write a self-contained HTML report of every figure.

    Plotly's bundle is inlined with the first figure and reused by the rest, so
    the file opens straight from disk and requests nothing off-machine.
    """
    latest = _latest_block(blocks)
    token_symbol = _symbol(latest, "token_symbol", "token")
    numeraire_symbol = _symbol(latest, "numeraire_symbol", "numeraire")
    robust_mid = _robust_mid(latest)
    figures = (
        cost_curve_figure(rows, numeraire_symbol=numeraire_symbol),
        book_map_figure(
            rows, robust_mid, token_symbol=token_symbol, numeraire_symbol=numeraire_symbol
        ),
        spread_curve_figure(rows, robust_mid, numeraire_symbol=numeraire_symbol),
        level_table_figure(rows),
        history_mid_figure(blocks, token_symbol=token_symbol, numeraire_symbol=numeraire_symbol),
        history_depth_figure(blocks, numeraire_symbol=numeraire_symbol),
        history_health_figure(blocks),
    )
    panels = "\n".join(
        '<div class="panel">'
        + plotly_io.to_html(
            figure,
            full_html=False,
            include_plotlyjs=index == 0,
            config=PLOTLY_CONFIG,
            default_height="420px",
        )
        + "</div>"
        for index, figure in enumerate(figures)
    )
    page_title = title or f"{token_symbol}/{numeraire_symbol} — measured depth"
    document = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(page_title)}</title>\n"
        f"<style>{REPORT_CSS}</style>\n</head>\n<body>\n"
        f'<div class="wrap">\n<h1>{html.escape(page_title)}</h1>\n'
        '<p class="sub">Every value is a Fynd quote or a direct function of quotes. '
        "No oracles, no estimates.</p>\n"
        f"{_summary_html(latest)}\n{panels}\n"
        f"<footer>{html.escape(_provenance_text(rows, blocks))}</footer>\n"
        "</div>\n</body>\n</html>\n"
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path
