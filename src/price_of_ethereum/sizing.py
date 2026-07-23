"""Trade-size sizing for depth sweeps.

The grid is denominated in a numeraire token (default USDC). A single Fynd probe
gives the traded token's price in the numeraire (`spot`), which converts each
numeraire notional into base-unit input amounts for both sweep directions:

  buy the token  (numeraire -> token):  input = notional in numeraire units
  sell the token (token -> numeraire):  input = notional / spot in token units

Both directions are sized from the *same* notional so bid and ask rungs pair by
notional. This generalizes to any pair: when the traded token is not the
numeraire, `spot` bridges through one extra probe quote.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import ValidationError

from price_of_ethereum.fynd.client import FyndClient, FyndError

# Default numeraire notional for the marginal spot probe.
DEFAULT_PROBE_NOTIONAL = 1000.0


class SpotProbeError(Exception):
    """The Fynd probe used to establish a spot price did not return a route."""


def atomic(units: float, decimals: int) -> int:
    """Convert human units to base (atomic) units, truncating toward zero.

    Scales via `Decimal` so the `10**decimals` factor stays exact for large
    notionals — plain `float * 10**decimals` loses integer precision above 2**53
    (e.g. $50M at 18 decimals), and these amounts feed signable quotes.
    """
    return int(Decimal(str(units)) * 10**decimals)


def numeraire_grid(min_notional: float, max_notional: float, samples: int) -> list[float]:
    """Log-spaced notionals from `min_notional` to `max_notional` inclusive."""
    if samples < 1:
        raise ValueError("samples must be >= 1")
    if samples == 1 or min_notional == max_notional:
        return [min_notional] * samples
    # Endpoints stay exp(log(x)) — a few ULP off the exact bounds — because the
    # reference collector computes them that way and the golden parity tests
    # pin its numbers; "fixing" the drift shifts endpoint rungs by one atomic
    # unit and breaks parity.
    low = math.log(min_notional)
    high = math.log(max_notional)
    return [math.exp(low + (high - low) * i / (samples - 1)) for i in range(samples)]


def spot_price(
    fynd: FyndClient,
    *,
    token: str,
    token_decimals: int,
    numeraire: str,
    numeraire_decimals: int,
    probe_notional: float = DEFAULT_PROBE_NOTIONAL,
) -> float:
    """Marginal price of `token` in numeraire units, from one probe quote that
    buys the token with `probe_notional` of numeraire (numeraire -> token)."""
    order = fynd.build_order(numeraire, token, atomic(probe_notional, numeraire_decimals))
    try:
        orders = fynd.quote(order, min_responses=1).orders
    except (FyndError, httpx.HTTPError, ValidationError) as error:
        raise SpotProbeError(f"spot probe request failed: {error}") from error
    if not orders:
        raise SpotProbeError("spot probe returned no orders")
    order_quote = orders[0]
    if order_quote.status != "success":
        raise SpotProbeError(f"spot probe returned status={order_quote.status}")
    try:
        token_out_units = int(order_quote.amount_out) / 10**token_decimals
    except ValueError as error:
        raise SpotProbeError(
            f"spot probe returned malformed amount_out={order_quote.amount_out!r}"
        ) from error
    if token_out_units <= 0:
        raise SpotProbeError("spot probe returned zero output")
    return probe_notional / token_out_units


@dataclass(frozen=True)
class SizedRung:
    """One grid point, sized for both sweep directions."""

    notional: float
    buy_amount: int  # numeraire base units, input to numeraire -> token
    sell_amount: int  # token base units, input to token -> numeraire


def size_rungs(
    grid: list[float],
    *,
    spot: float,
    token_decimals: int,
    numeraire_decimals: int,
) -> list[SizedRung]:
    """Size each numeraire notional into both-direction input amounts."""
    if spot <= 0:
        raise ValueError("spot must be positive")
    rungs: list[SizedRung] = []
    for notional in grid:
        buy_amount = atomic(notional, numeraire_decimals)
        sell_amount = atomic(notional / spot, token_decimals)
        if buy_amount == 0 or sell_amount == 0:
            raise ValueError(
                f"notional {notional} truncates to a zero base-unit amount "
                f"(buy={buy_amount}, sell={sell_amount}); raise the grid minimum"
            )
        rungs.append(SizedRung(notional=notional, buy_amount=buy_amount, sell_amount=sell_amount))
    return rungs
