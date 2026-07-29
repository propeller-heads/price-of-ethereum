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
from price_of_ethereum.fynd.models import QUOTE_STATUS_SUCCESS
from price_of_ethereum.pricing import Side

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


def sized_amount(
    notional: float,
    *,
    side: Side,
    spot: float,
    token_decimals: int,
    numeraire_decimals: int,
) -> int:
    """Base-unit input amount for one numeraire notional in one direction.

    The single place a notional becomes a signable amount: rungs, anchored
    bisection probes, and mid probes all size through here, so a notional that
    appears twice can never be sized two different ways.
    """
    if side == "buy":
        return atomic(notional, numeraire_decimals)
    return atomic(notional / spot, token_decimals)


def numeraire_grid(min_notional: float, max_notional: float, samples: int) -> list[float]:
    """Log-spaced notionals from `min_notional` to `max_notional` inclusive."""
    if samples < 1:
        raise ValueError("samples must be >= 1")
    if samples == 1 or min_notional == max_notional:
        return [min_notional] * samples
    # Endpoints are exp(log(x)), a few ULP off the exact bounds. Snapping them to
    # the round number would shift those rungs by an atomic unit and change every
    # recorded price at the ends of the grid, so the drift is kept deliberately.
    low = math.log(min_notional)
    high = math.log(max_notional)
    return [math.exp(low + (high - low) * i / (samples - 1)) for i in range(samples)]


def _probe_price(
    fynd: FyndClient,
    *,
    token: str,
    token_decimals: int,
    numeraire: str,
    numeraire_decimals: int,
    probe_notional: float,
) -> tuple[float, int | None]:
    """One quote spending `probe_notional` of numeraire on token, as the price in
    numeraire units per token unit and the block Fynd solved it against."""
    order = fynd.build_order(numeraire, token, atomic(probe_notional, numeraire_decimals))
    try:
        orders = fynd.quote(order, min_responses=1, encoding=False).orders
    except (FyndError, httpx.HTTPError, ValidationError) as error:
        raise SpotProbeError(f"spot probe request failed: {error}") from error
    if not orders:
        raise SpotProbeError("spot probe returned no orders")
    order_quote = orders[0]
    if order_quote.status != QUOTE_STATUS_SUCCESS:
        raise SpotProbeError(f"spot probe returned status={order_quote.status}")
    try:
        token_out_units = int(order_quote.amount_out) / 10**token_decimals
    except ValueError as error:
        raise SpotProbeError(
            f"spot probe returned malformed amount_out={order_quote.amount_out!r}"
        ) from error
    if token_out_units <= 0:
        raise SpotProbeError("spot probe returned zero output")
    return probe_notional / token_out_units, order_quote.block.number


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
    price, _ = _probe_price(
        fynd,
        token=token,
        token_decimals=token_decimals,
        numeraire=numeraire,
        numeraire_decimals=numeraire_decimals,
        probe_notional=probe_notional,
    )
    return price


@dataclass(frozen=True)
class ReferenceRate:
    """What one numeraire unit is worth in reference units, quoted both ways.

    `rate` is the midpoint of the two sides. `spread` is `(ask - bid) / rate`,
    the round-trip cost at the probe size, which says whether the reference pair
    is deep enough for `rate` to mean anything. `block` is what Fynd solved the
    first leg against, so a reader can see how far a run has moved since.
    """

    rate: float
    spread: float
    block: int | None


def reference_rate(
    fynd: FyndClient,
    *,
    numeraire: str,
    numeraire_decimals: int,
    reference: str,
    reference_decimals: int,
    probe_notional: float = DEFAULT_PROBE_NOTIONAL,
) -> ReferenceRate:
    """Price the numeraire against a reference token from both sides.

    One quote buys the numeraire with `probe_notional` of reference and one sells
    an equivalent amount back. A single quote would give an ask — inflated by the
    spread, the router's fee and whatever impact the probe itself causes — and no
    way to tell a deep pair from a thin one, because any positive number looks
    like a price.
    """
    ask, block = _probe_price(
        fynd,
        token=numeraire,
        token_decimals=numeraire_decimals,
        numeraire=reference,
        numeraire_decimals=reference_decimals,
        probe_notional=probe_notional,
    )
    numeraire_back, _ = _probe_price(
        fynd,
        token=reference,
        token_decimals=reference_decimals,
        numeraire=numeraire,
        numeraire_decimals=numeraire_decimals,
        probe_notional=probe_notional / ask,
    )
    bid = 1.0 / numeraire_back
    mid = (ask + bid) / 2.0
    if not math.isfinite(mid) or mid <= 0:
        raise SpotProbeError(f"reference probe produced an unusable rate: {mid!r}")
    return ReferenceRate(rate=mid, spread=(ask - bid) / mid, block=block)


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
        buy_amount = sized_amount(
            notional,
            side="buy",
            spot=spot,
            token_decimals=token_decimals,
            numeraire_decimals=numeraire_decimals,
        )
        sell_amount = sized_amount(
            notional,
            side="sell",
            spot=spot,
            token_decimals=token_decimals,
            numeraire_decimals=numeraire_decimals,
        )
        if buy_amount == 0 or sell_amount == 0:
            raise ValueError(
                f"notional {notional} truncates to a zero base-unit amount "
                f"(buy={buy_amount}, sell={sell_amount}); raise the grid minimum"
            )
        rungs.append(SizedRung(notional=notional, buy_amount=buy_amount, sell_amount=sell_amount))
    return rungs
