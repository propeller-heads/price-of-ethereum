"""Plotly figures for a recorded measurement.

Each builder takes the JSONL as a DataFrame and returns a figure; nothing here
writes a file or assembles a page. Axis titles carry the pair's own symbols and
the band comes from the run that produced it, because a size axis is in
numeraire units and those are only dollars when the numeraire is one.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import pandas as pd

try:
    import plotly.graph_objects as go
except ImportError as error:  # pragma: no cover - exercised by the CLI guard
    raise ImportError(
        "the figures need the viz extra: pip install 'price-of-ethereum[viz]' "
        "(or: uv sync --extra viz)"
    ) from error

from price_of_ethereum.pricing import ROBUST_MID_MAX_DEPTH, ROBUST_MID_MIN_DEPTH

# Plotly renders a pseudo-HTML dialect inside titles, axis labels and trace
# names, so `<a href>`, `<span style>` and `<b>` survive JSON encoding and are
# drawn as markup by the browser. A token symbol is on-chain metadata that
# anyone can mint, so it reaches these figures as untrusted text and is stripped
# here rather than at a call site, which would only protect the callers that
# remembered.
FIGURE_MARKUP = re.compile(r"[<>]|[\x00-\x1f\x7f-\x9f]")


def figure_text(value: str) -> str:
    """A caller-supplied string with anything Plotly would draw as markup removed."""
    return FIGURE_MARKUP.sub("", value)


# Which tick formatting an x axis needs; see `_base_layout`.
XAxisKind = Literal["linear", "size", "block"]

# Colorblind-safe pair; buy/ask warm, sell/bid cool, held consistent everywhere.
BUY_COLOR = "#d1610b"
SELL_COLOR = "#1f6fb4"
NEUTRAL_COLOR = "#7a7a7a"
FLAG_COLOR = "#a8324a"


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
    numeraire_symbol = figure_text(numeraire_symbol)

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
    band_min: float = ROBUST_MID_MIN_DEPTH,
    band_max: float = ROBUST_MID_MAX_DEPTH,
) -> go.Figure:
    """Effective execution price per side, with the robust-mid band shaded.

    The band is in numeraire units, which are dollars only when the numeraire is
    a dollar. The defaults suit that case; a run against another numeraire
    records the band it actually used and passes it in.
    """
    token_symbol = figure_text(token_symbol)
    numeraire_symbol = figure_text(numeraire_symbol)

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
        x0=band_min,
        x1=band_max,
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
    numeraire_symbol = figure_text(numeraire_symbol)

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
    """Robust mid and spot across every recorded block."""
    token_symbol = figure_text(token_symbol)
    numeraire_symbol = figure_text(numeraire_symbol)

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
