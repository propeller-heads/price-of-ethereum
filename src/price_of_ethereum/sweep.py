"""Depth sweep against Fynd: bulk log-grid fan-out, anchored bisection toward
headline impact targets, and per-target level derivation.

The bulk sweep quotes with `min_responses=1` — return on the first, fast
bellman_ford response — so a full two-sided sweep fits inside a block interval.
Anchors re-quote a handful of headline levels with `min_responses=0` — wait for
every solver pool — so the path_frank_wolfe split solver contributes routes.
Every request carries exactly one order; concurrency comes from the thread pool,
never from batching (each quote needs its own `min_responses`).

Only anchor quotes ask Fynd to encode: calldata and a fee breakdown are kept for
the headline levels and nothing else reads them, while `amount_out` — the only
field the sweep measures — is identical either way.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import ValidationError

from price_of_ethereum.fynd.client import FyndClient, FyndError
from price_of_ethereum.fynd.models import QUOTE_STATUS_SUCCESS, OrderQuote
from price_of_ethereum.pricing import Side, execution_price, impact_pct, is_finite_number
from price_of_ethereum.sizing import SizedRung, sized_amount
from price_of_ethereum.tokens import TokenMeta

logger = logging.getLogger(__name__)

DerivedFrom = Literal["nearest_real_quote", "anchored_bisection"]
Bound = Literal["min", "max"]


@dataclass(frozen=True)
class SweepPoint:
    """One measured sweep rung. `notional` is rounded to cents, prices and
    impact to 6 decimals — the granularity the dataset publishes."""

    notional: float
    price: float
    impact_pct: float
    quote: OrderQuote
    solve_time_ms: int


@dataclass(frozen=True)
class AnchorResult:
    """Best bisection quote near an impact target; values unrounded."""

    notional: float
    price: float
    impact_pct: float
    quote: OrderQuote
    solve_time_ms: int


@dataclass(frozen=True)
class Level:
    """Per-target depth level, derived from a real quote (never interpolated).

    `bound` is None when the sweep crossed the target, "max" when even the
    largest size stayed below it, "min" when even the smallest size exceeded it.
    """

    side: Side
    target_impact_pct: float
    actual_impact_pct: float
    target_reached: bool
    bound: Bound | None
    notional: float
    price: float
    quote: OrderQuote
    solve_time_ms: int
    derived_from: DerivedFrom


def quote_at_notional(
    fynd: FyndClient,
    *,
    side: Side,
    notional: float,
    amount: int,
    spot: float,
    token: TokenMeta,
    numeraire: TokenMeta,
    min_responses: int,
    timeout_ms: int,
    encoding: bool,
) -> tuple[OrderQuote, float, int] | None:
    """Quote `amount` base units in `side` direction; (quote, execution_price,
    solve_time_ms). `notional` is the numeraire size `amount` was sized from and
    only prices the result — callers size through `sizing.sized_amount`.

    Returns None on transport errors, non-success status, or an unusable price —
    sweep and anchor callers skip failures rather than abort.
    """
    if side == "buy":
        token_in, token_out = numeraire.address, token.address
    else:
        token_in, token_out = token.address, numeraire.address
    order = fynd.build_order(token_in, token_out, amount)
    try:
        result = fynd.quote(
            order, min_responses=min_responses, timeout_ms=timeout_ms, encoding=encoding
        )
    except (FyndError, httpx.HTTPError, ValidationError) as error:
        logger.debug("quote failed (%s %s notional): %s", side, notional, error)
        return None
    if not result.orders:
        logger.debug("quote returned no orders (%s %s notional)", side, notional)
        return None
    order_quote = result.orders[0]
    if order_quote.status != QUOTE_STATUS_SUCCESS:
        logger.debug("quote status=%s (%s %s notional)", order_quote.status, side, notional)
        return None
    price = execution_price(
        side=side,
        amount_out=order_quote.amount_out,
        notional=notional,
        token_decimals=token.decimals,
        numeraire_decimals=numeraire.decimals,
        spot=spot,
    )
    if price is None or not is_finite_number(price):
        logger.debug("quote price unusable (%s %s notional): %r", side, notional, price)
        return None
    return order_quote, price, result.solve_time_ms


def sweep_side(
    fynd: FyndClient,
    *,
    side: Side,
    rungs: list[SizedRung],
    spot: float,
    token: TokenMeta,
    numeraire: TokenMeta,
    max_workers: int = 6,
    timeout_ms: int = 8000,
) -> list[SweepPoint]:
    """Fan out one quote per rung (`min_responses=1`), sorted ascending by
    notional. Failed rungs are skipped."""

    def measure(rung: SizedRung) -> SweepPoint | None:
        measured = quote_at_notional(
            fynd,
            side=side,
            notional=rung.notional,
            amount=rung.buy_amount if side == "buy" else rung.sell_amount,
            spot=spot,
            token=token,
            numeraire=numeraire,
            min_responses=1,
            timeout_ms=timeout_ms,
            encoding=False,
        )
        if measured is None:
            return None
        order_quote, price, solve_time_ms = measured
        return SweepPoint(
            notional=round(rung.notional, 2),
            price=round(price, 6),
            impact_pct=round(impact_pct(price, spot, side), 6),
            quote=order_quote,
            solve_time_ms=solve_time_ms,
        )

    points: list[SweepPoint] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(measure, rung) for rung in rungs]
        for future in as_completed(futures):
            point = future.result()
            if point is not None:
                points.append(point)
    points.sort(key=lambda point: point.notional)
    return points


def anchor_target_from_sweep(
    fynd: FyndClient,
    *,
    side: Side,
    target_pct: float,
    sweep: list[SweepPoint],
    spot: float,
    token: TokenMeta,
    numeraire: TokenMeta,
    timeout_ms: int = 8000,
    max_iters: int = 3,
    tolerance: float = 0.02,
) -> AnchorResult | None:
    """Tight bisection seeded by the sweep's bracket around `target_pct`.

    Each iteration is a slow encoded split quote (`min_responses=0`) so the
    returned measurement carries path_frank_wolfe routes and the calldata that
    proves it. Returns None when the sweep never straddles the target (capped or
    failed) — the sweep-derived level stands as-is.
    """
    if len(sweep) < 2:
        return None
    bracket: tuple[SweepPoint, SweepPoint] | None = None
    for index in range(len(sweep) - 1):
        delta_low = sweep[index].impact_pct - target_pct
        delta_high = sweep[index + 1].impact_pct - target_pct
        if delta_low == 0 or (delta_low < 0 <= delta_high) or (delta_low > 0 >= delta_high):
            bracket = (sweep[index], sweep[index + 1])
            break
    if bracket is None:
        return None

    low_notional = bracket[0].notional
    high_notional = bracket[1].notional
    best: AnchorResult | None = None
    best_diff = float("inf")
    for _ in range(max_iters):
        mid_notional = math.exp((math.log(low_notional) + math.log(high_notional)) / 2)
        measured = quote_at_notional(
            fynd,
            side=side,
            notional=mid_notional,
            amount=sized_amount(
                mid_notional,
                side=side,
                spot=spot,
                token_decimals=token.decimals,
                numeraire_decimals=numeraire.decimals,
            ),
            spot=spot,
            token=token,
            numeraire=numeraire,
            min_responses=0,
            timeout_ms=timeout_ms,
            encoding=True,
        )
        if measured is None:
            break
        order_quote, price, solve_time_ms = measured
        actual_impact = impact_pct(price, spot, side)
        diff = abs(actual_impact - target_pct)
        if diff < best_diff:
            best = AnchorResult(
                notional=mid_notional,
                price=price,
                impact_pct=actual_impact,
                quote=order_quote,
                solve_time_ms=solve_time_ms,
            )
            best_diff = diff
        if diff / max(target_pct, 0.001) < tolerance:
            break
        if actual_impact < target_pct:
            low_notional = mid_notional
        else:
            high_notional = mid_notional
    return best


def derive_level_from_sweep(
    sweep: list[SweepPoint],
    *,
    side: Side,
    target_pct: float,
) -> Level | None:
    """Per-target level from the nearest real sweep quote around the crossing.

    Measured impact is not strictly monotonic in size (route recomposition can
    dip impact as size grows), so scan for sign changes of (impact - target)
    between adjacent points and take the first crossing — "how much can you
    trade before X%". One exception: when the smallest rung already exceeds the
    target, the level is a min bound even if a later dip crosses back below —
    nothing tradeable sits under the target. None when the sweep is empty.
    """
    if not sweep:
        return None

    def from_point(point: SweepPoint, *, bound: Bound | None, target_reached: bool) -> Level:
        return Level(
            side=side,
            target_impact_pct=target_pct,
            actual_impact_pct=point.impact_pct,
            target_reached=target_reached,
            bound=bound,
            notional=point.notional,
            price=point.price,
            quote=point.quote,
            solve_time_ms=point.solve_time_ms,
            derived_from="nearest_real_quote",
        )

    for index in range(len(sweep) - 1):
        first, second = sweep[index], sweep[index + 1]
        delta_first = first.impact_pct - target_pct
        delta_second = second.impact_pct - target_pct
        if delta_first > 0 and index == 0:
            return from_point(first, bound="min", target_reached=False)
        crossed_up = delta_first < 0 <= delta_second
        crossed_down = delta_first > 0 >= delta_second
        if delta_first == 0 or crossed_up or crossed_down:
            impact_span = second.impact_pct - first.impact_pct
            position = (target_pct - first.impact_pct) / impact_span if impact_span else 0.0
            closer = first if position < 0.5 else second
            return from_point(closer, bound=None, target_reached=True)

    if sweep[-1].impact_pct < target_pct:
        return from_point(sweep[-1], bound="max", target_reached=False)
    if sweep[0].impact_pct > target_pct:
        return from_point(sweep[0], bound="min", target_reached=False)
    return None


def level_from_anchor(
    anchor: AnchorResult,
    *,
    side: Side,
    target_pct: float,
    tolerance: float,
) -> Level:
    """Replace a sweep-derived level with the anchored bisection measurement."""
    within = abs(anchor.impact_pct - target_pct) / max(target_pct, 0.001) < tolerance
    bound: Bound | None = None if within else ("max" if anchor.impact_pct < target_pct else "min")
    return Level(
        side=side,
        target_impact_pct=target_pct,
        actual_impact_pct=round(anchor.impact_pct, 6),
        target_reached=within,
        bound=bound,
        notional=round(anchor.notional, 2),
        price=round(anchor.price, 6),
        quote=anchor.quote,
        solve_time_ms=anchor.solve_time_ms,
        derived_from="anchored_bisection",
    )
