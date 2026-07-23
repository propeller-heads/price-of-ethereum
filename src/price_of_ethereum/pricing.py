"""Pure pricing math: execution price, impact, and the robust mid.

Prices are numeraire units per token unit. `side` names the direction of the
underlying quote: "buy" = numeraire -> token, "sell" = token -> numeraire.

Math is float on purpose. These are display-grade measurements — exact to well
past the 6 decimals they round to (float64 is exact to 2**53; the largest grid
size is 5e13 atomic numeraire units) — and never amounts that sign or settle a
transaction. Float keeps bit-for-bit parity with the marketprice.xyz collector,
which the golden tests pin.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from typing import Literal

Side = Literal["buy", "sell"]

# Numeraire depth band whose two-sided midpoints define the robust mid. The mid
# should represent the current clearing price, not liquidity at six- or
# seven-figure depth, so it comes from the shallow reliable band.
ROBUST_MID_MIN_DEPTH = 2_500.0
ROBUST_MID_MAX_DEPTH = 10_000.0
ROBUST_MID_TARGET_DEPTH = 5_000.0
ROBUST_MID_SAMPLES = 5


def is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


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


def impact_pct(price: float, spot: float, side: Side) -> float:
    """Signed price impact in percent vs `spot`; worse-than-spot is positive."""
    if side == "buy":
        return (price / spot - 1.0) * 100.0
    return (1.0 - price / spot) * 100.0


def derive_price_impact_bps(effective_price: float | None, mid_price: float | None) -> float | None:
    """Signed price impact in basis points vs the robust mid.

    Buy above mid is positive cost, sell below mid is negative cost; one formula
    across sides keeps the sign convention right.
    """
    if not effective_price or not mid_price:
        return None
    return round((effective_price / mid_price - 1.0) * 10000.0, 1)


def choose_robust_mid(depth_mid_pairs: Iterable[tuple[float, float]]) -> tuple[float, float] | None:
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

    band = [pair for pair in clean if ROBUST_MID_MIN_DEPTH <= pair[0] <= ROBUST_MID_MAX_DEPTH]
    if len(band) < 3:
        band = sorted(
            clean,
            key=lambda pair: abs(math.log(pair[0] / ROBUST_MID_TARGET_DEPTH)),
        )[:ROBUST_MID_SAMPLES]

    mids = [mid for _, mid in band]
    median_mid = statistics.median(mids)
    median_depth = min(band, key=lambda pair: abs(pair[1] - median_mid))[0]
    return round(median_mid, 6), round(median_depth, 2)


def robust_mid_from_sides(
    buy_points: Iterable[tuple[float, float]],
    sell_points: Iterable[tuple[float, float]],
) -> tuple[float, float] | None:
    """Robust mid from already-collected sweep rungs, as (notional, price) pairs.

    Buy and sell rungs pair by notional rounded to cents (both sides quote the
    same numeraire grid); each pair's midpoint feeds `choose_robust_mid`.
    """
    buy_price_by_depth: dict[float, float] = {}
    for depth, price in buy_points:
        if is_finite_number(depth) and is_finite_number(price):
            buy_price_by_depth[round(float(depth), 2)] = float(price)

    pairs: list[tuple[float, float]] = []
    for depth, sell_price in sell_points:
        if not is_finite_number(depth) or not is_finite_number(sell_price):
            continue
        buy_price = buy_price_by_depth.get(round(float(depth), 2))
        if buy_price is None:
            continue
        pairs.append((float(depth), (buy_price + float(sell_price)) / 2.0))

    return choose_robust_mid(pairs)


def robust_mid_probe_depths(max_depth: float) -> list[float]:
    """Log-spaced dedicated-probe depths across the band, capped at `max_depth`."""
    capped = max(ROBUST_MID_MIN_DEPTH, min(max_depth, ROBUST_MID_MAX_DEPTH))
    if capped <= ROBUST_MID_MIN_DEPTH:
        return [ROBUST_MID_MIN_DEPTH]
    return [
        math.exp(
            math.log(ROBUST_MID_MIN_DEPTH)
            + i * (math.log(capped) - math.log(ROBUST_MID_MIN_DEPTH)) / (ROBUST_MID_SAMPLES - 1)
        )
        for i in range(ROBUST_MID_SAMPLES)
    ]
