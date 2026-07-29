"""Pure pricing math: execution price, impact, and the robust mid.

Prices are numeraire units per token unit. `side` names the direction of the
underlying quote: "buy" = numeraire -> token, "sell" = token -> numeraire.

Math is float on purpose: these are measurements for display and analysis,
exact well past the significant figures they publish at (float64 holds integers
exactly to 2**53, and the largest grid size is 5e13 atomic numeraire units).
Amounts that sign or settle a transaction are sized in `sizing.py` with
`Decimal` instead.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from typing import Literal

Side = Literal["buy", "sell"]

# The depth band whose two-sided midpoints define the robust mid. The mid should
# represent the current clearing price, not liquidity at six- or seven-figure
# depth, so it comes from the shallow reliable band.
#
# These are dollars. They are also the defaults in *numeraire* units, which is
# only the same thing when the numeraire is a dollar stablecoin. Callers measuring
# against another numeraire scale them by a measured rate and pass the result in;
# see `SnapshotConfig.mid_band_min`.
ROBUST_MID_MIN_DEPTH = 2_500.0
ROBUST_MID_MAX_DEPTH = 10_000.0
ROBUST_MID_SAMPLES = 5

# Published sizes and prices keep this many significant figures. Decimal places
# cannot serve both a numeraire worth a dollar and one worth $118,000: two of
# them resolve a $50 rung to 0.002% and a 0.000424 WBTC rung to nothing at all.
# Significant figures resolve the same fraction of any value, which is what a
# measurement across arbitrary pairs needs.
PUBLISHED_DIGITS = 9

# Buy and sell rungs pair by notional, and both sides are sized from the same
# grid value, so they agree to well within this. It is deliberately coarser than
# what gets published: the key only has to absorb float noise between two paths
# to the same number, and the grid's own steps are ~15% apart.
PAIRING_DIGITS = 6


def is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def significant(value: float, digits: int = PUBLISHED_DIGITS) -> float:
    """Round to `digits` significant figures, so precision follows scale."""
    if not is_finite_number(value) or value == 0.0:
        return value
    return float(f"{value:.{digits}g}")


def execution_price(
    *,
    side: Side,
    amount_out: str,
    notional: float,
    token_decimals: int,
    numeraire_decimals: int,
    spot: float | None = None,
) -> float | None:
    """Effective numeraire-per-token price of one quote at `notional`.

    buy: `notional` numeraire in, token out — price = notional / token_out_units.
    sell: `notional / spot` token in, numeraire out — price = numeraire_out_units
    / token_in_units. `spot` is required for sell because the input amount was
    sized from it.
    """
    try:
        amount_out_atomic = int(amount_out)
    except ValueError:
        # amount_out is a decimal string from an external server; a malformed
        # value degrades this one quote to "no usable price", never a crash.
        return None
    if side == "buy":
        token_out_units = amount_out_atomic / 10**token_decimals
        return notional / token_out_units if token_out_units > 0 else None
    if spot is None or spot <= 0:
        raise ValueError("sell-side execution price requires a positive spot")
    numeraire_out_units = amount_out_atomic / 10**numeraire_decimals
    token_in_units = notional / spot
    return numeraire_out_units / token_in_units if token_in_units > 0 else None


def impact_pct(price: float, reference: float, side: Side) -> float:
    """Price impact in percent against `reference`, on the cost convention.

    A price worse than the reference is positive on both sides: a buy pays above
    it, a sell receives below it. `reference` must be two-sided — the robust mid,
    not a one-directional quote — or the whole spread lands on the sell side as
    impact it did not cause.
    """
    if side == "buy":
        return (price / reference - 1.0) * 100.0
    return (1.0 - price / reference) * 100.0


def derive_price_impact_bps(
    effective_price: float | None, mid_price: float | None, side: Side
) -> float | None:
    """Price impact in basis points against the robust mid.

    The same measurement as `impact_pct` in a hundredth of the unit and on the
    same cost convention, rounded to a tenth of a basis point.
    """
    if not effective_price or not mid_price:
        return None
    return round(impact_pct(effective_price, mid_price, side) * 100.0, 1)


def choose_robust_mid(
    depth_mid_pairs: Iterable[tuple[float, float]],
    *,
    band_min: float = ROBUST_MID_MIN_DEPTH,
    band_max: float = ROBUST_MID_MAX_DEPTH,
) -> tuple[float, float] | None:
    """Median two-sided midpoint in the depth band; returns (mid, median_depth).

    When fewer than 3 pairs land inside the band, take the ROBUST_MID_SAMPLES
    pairs log-nearest to the target depth instead. `median_depth` is the depth
    whose midpoint sits closest to the median.
    """
    clean = [
        (float(depth), float(mid))
        for depth, mid in depth_mid_pairs
        if is_finite_number(depth) and depth > 0 and is_finite_number(mid)
    ]
    if not clean:
        return None

    target = math.sqrt(band_min * band_max)
    band = [pair for pair in clean if band_min <= pair[0] <= band_max]
    if len(band) < 3:
        band = sorted(
            clean,
            key=lambda pair: abs(math.log(pair[0] / target)),
        )[:ROBUST_MID_SAMPLES]

    mids = [mid for _, mid in band]
    median_mid = statistics.median(mids)
    median_depth = min(band, key=lambda pair: abs(pair[1] - median_mid))[0]
    return significant(median_mid), significant(median_depth)


def robust_mid_from_sides(
    buy_points: Iterable[tuple[float, float]],
    sell_points: Iterable[tuple[float, float]],
    *,
    band_min: float = ROBUST_MID_MIN_DEPTH,
    band_max: float = ROBUST_MID_MAX_DEPTH,
) -> tuple[float, float] | None:
    """Robust mid from already-collected sweep rungs, as (notional, price) pairs.

    Buy and sell rungs pair on notional to `PAIRING_DIGITS` figures (both sides
    quote the same numeraire grid); each pair's midpoint feeds
    `choose_robust_mid`.
    """
    buy_price_by_depth: dict[float, float] = {}
    for depth, price in buy_points:
        if is_finite_number(depth) and is_finite_number(price):
            buy_price_by_depth[significant(float(depth), PAIRING_DIGITS)] = float(price)

    pairs: list[tuple[float, float]] = []
    for depth, sell_price in sell_points:
        if not is_finite_number(depth) or not is_finite_number(sell_price):
            continue
        buy_price = buy_price_by_depth.get(significant(float(depth), PAIRING_DIGITS))
        if buy_price is None:
            continue
        pairs.append((float(depth), (buy_price + float(sell_price)) / 2.0))

    return choose_robust_mid(pairs, band_min=band_min, band_max=band_max)


def robust_mid_probe_depths(
    max_depth: float,
    *,
    band_min: float = ROBUST_MID_MIN_DEPTH,
    band_max: float = ROBUST_MID_MAX_DEPTH,
) -> list[float]:
    """Log-spaced dedicated-probe depths across the band, capped at `max_depth`."""
    capped = max(band_min, min(max_depth, band_max))
    if capped <= band_min:
        return [band_min]
    return [
        math.exp(
            math.log(band_min)
            + i * (math.log(capped) - math.log(band_min)) / (ROBUST_MID_SAMPLES - 1)
        )
        for i in range(ROBUST_MID_SAMPLES)
    ]
