"""Assemble one block's depth snapshot: two-sided sweep, majority-block
reconciliation, robust mid, and anchored levels.

Block identity comes from the quotes themselves — the majority block among the
sweep's parsed `OrderQuote.block.number` values labels the snapshot (no RPC
clock). Quotes on a *different* parsed block are excluded from the mid and from
`to_rows()` and flag the snapshot `mixed_block`, while quotes with no parseable
block are kept.

Every impact the snapshot publishes is measured against `robust_mid`, the
two-sided midpoint of the shallow band. `spot` — a single numeraire -> token
probe, and therefore an ask — only sizes trades. Measuring impact against it
would bill the sell side for the whole bid/ask spread on top of its own impact,
so the two are deliberately kept to their own jobs.
"""

from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

from price_of_ethereum.fynd.client import DEFAULT_SLIPPAGE, DUMMY_SENDER, FyndClient
from price_of_ethereum.fynd.models import OrderQuote, Transaction
from price_of_ethereum.pricing import (
    ROBUST_MID_MAX_DEPTH,
    ROBUST_MID_MIN_DEPTH,
    choose_robust_mid,
    derive_price_impact_bps,
    robust_mid_from_sides,
    robust_mid_probe_depths,
)
from price_of_ethereum.sizing import numeraire_grid, size_rungs, sized_amount, spot_price
from price_of_ethereum.sweep import (
    AnchorResult,
    Level,
    MeasuredRung,
    Side,
    SweepPoint,
    anchor_target_from_sweep,
    derive_level_from_sweep,
    level_from_anchor,
    quote_at_notional,
    reference_sweep,
    sweep_side,
)
from price_of_ethereum.tokens import TokenMeta

logger = logging.getLogger(__name__)

DEFAULT_IMPACT_LEVELS = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5,
    2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 25.0, 35.0, 50.0,
)  # fmt: skip
DEFAULT_ANCHOR_TARGETS = (0.5, 1.0, 5.0, 10.0, 25.0, 50.0)

MidSource = Literal["sweep_band", "probe_fallback", "spot_degraded"]

TenderlyStatus = Literal["ready", "placeholder_sender", "missing_sender", "no_transaction"]

TENDERLY_SIMULATOR = "https://dashboard.tenderly.co/simulator/new"


@dataclass(frozen=True)
class SnapshotConfig:
    """Per-pair collection parameters.

    `samples_per_side` is pinned at 100 by default and is NOT a free resolution
    knob: it decides which rungs land in the robust-mid depth band, so changing
    it moves `robust_mid`.
    """

    token: TokenMeta
    numeraire: TokenMeta
    pair: str
    chain_id: int
    search_min: float = 50.0
    search_max: float = 50_000_000.0
    samples_per_side: int = 100
    impact_levels: tuple[float, ...] = DEFAULT_IMPACT_LEVELS
    anchor_targets: tuple[float, ...] = DEFAULT_ANCHOR_TARGETS
    max_workers: int = 6
    timeout_ms: int = 8000
    anchor_max_iters: int = 3
    anchor_bisect_tolerance: float = 0.02
    anchor_accept_tolerance: float = 0.05
    probe_notional: float = 1000.0
    # The robust-mid band, in numeraire units. The defaults read as dollars and
    # are only dollars when the numeraire is a dollar stablecoin; `cli` scales
    # them by a measured rate when it is not.
    mid_band_min: float = ROBUST_MID_MIN_DEPTH
    mid_band_max: float = ROBUST_MID_MAX_DEPTH
    # Dollars per numeraire unit, measured through Fynd, or None when sizes are
    # raw numeraire units. Recorded so a reader knows what the band meant. It is
    # measured once, because a band that moved between blocks would make them
    # incomparable, so `numeraire_usd_block` carries how old it is.
    numeraire_usd: float | None = None
    numeraire_usd_block: int | None = None
    # Round-trip cost of the pair the rate was measured on, in basis points. A
    # rate taken across 8 bps and one admitted at the edge of the tolerance are
    # worth different amounts of trust, and only this says which happened.
    numeraire_usd_spread_bps: float | None = None
    # Encoding slippage as a decimal string, Fynd's wire type. Only anchored
    # levels encode, and the default is tight enough that Fynd's price guard
    # refuses the largest sizes — loosen it to get calldata for those.
    slippage: str = DEFAULT_SLIPPAGE

    def __post_init__(self) -> None:
        # An anchor target absent from impact_levels would still run its full
        # bisection (slow min_responses=0 quotes) and then have nowhere to land.
        orphaned = [target for target in self.anchor_targets if target not in self.impact_levels]
        if orphaned:
            raise ValueError(f"anchor_targets {orphaned} are not in impact_levels")


def parse_quote_block(quote: OrderQuote) -> int | None:
    """Block number a quote was solved against; None when Fynd sent no usable one."""
    return quote.block.number if quote.block.number > 0 else None


def build_tenderly_url(
    *,
    sender: str | None,
    transaction: Transaction | None,
    chain_id: int,
    block_number: int | None,
) -> tuple[str | None, TenderlyStatus]:
    """One-click simulation link for an encoded quote, plus how far to trust it.

    The status names the reason explicitly instead of leaving a bare null. A
    quote Fynd never encoded is `no_transaction` and an encoded quote with
    nobody to simulate from is `missing_sender`, both without a link.
    `placeholder_sender` still carries a link, but it simulates from the
    quote-only placeholder address, which holds no balance and has approved
    nothing — the calldata is real, the simulation will revert on the transfer.
    Only `ready` means the link simulates the trade someone could execute; pass
    `--sender` with a funded address to get one.
    """
    if transaction is None or not transaction.to or not transaction.data:
        return None, "no_transaction"
    if not sender:
        return None, "missing_sender"
    params = {
        "network": str(chain_id),
        "from": sender,
        "contractAddress": transaction.to,
        "rawFunctionInput": transaction.data,
        "value": transaction.value or "0",
    }
    if block_number is not None:
        params["block"] = str(block_number)
    url = f"{TENDERLY_SIMULATOR}?{urlencode(params)}"
    if sender.lower() == DUMMY_SENDER.lower():
        return url, "placeholder_sender"
    return url, "ready"


def route_hash(quote: OrderQuote) -> str | None:
    """Stable route fingerprint: sha256 over component_ids sorted ascending."""
    if quote.route is None or not quote.route.swaps:
        return None
    component_ids = sorted({swap.component_id for swap in quote.route.swaps})
    return hashlib.sha256(",".join(component_ids).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Snapshot:
    """One block's measured depth snapshot for a pair."""

    pair: str
    chain_id: int
    token: TokenMeta
    numeraire: TokenMeta
    block_number: int | None
    block_hash: str | None
    block_timestamp: int | None
    gas_price_wei: str | None
    mixed_block: bool
    spot: float
    robust_mid: float
    median_depth: float
    mid_source: MidSource
    curve_buy: list[SweepPoint]
    curve_sell: list[SweepPoint]
    levels: list[Level]
    search_min: float
    search_max: float
    samples_per_side: int
    mid_band_min: float
    mid_band_max: float
    numeraire_usd: float | None
    numeraire_usd_block: int | None
    numeraire_usd_spread_bps: float | None
    slippage: str
    duration_ms: int

    def _matches_block(self, quote: OrderQuote) -> bool:
        number = parse_quote_block(quote)
        return number is None or self.block_number is None or number == self.block_number

    def _row(
        self,
        *,
        kind: str,
        side: Side,
        notional: float,
        price: float,
        impact: float,
        quote: OrderQuote,
        solve_time_ms: int,
    ) -> dict[str, Any]:
        # A quote's gas cost is deducted from what it pays out, so it lands in
        # whichever token the trade outputs — the token on a buy, the numeraire
        # on a sell. The row names that token rather than leaving one column
        # holding two currencies with nothing to tell them apart.
        out_token = self.token if side == "buy" else self.numeraire
        out_decimals = out_token.decimals
        # Gas fields are decimal strings from an external server; malformed
        # values null the derived gas columns rather than losing the row.
        gas_cost_token_out: float | None = None
        try:
            net_gas_diff = int(quote.amount_out) - int(quote.amount_out_net_gas)
        except ValueError:
            net_gas_diff = 0
        if net_gas_diff > 0:
            gas_cost_token_out = net_gas_diff / 10**out_decimals
        try:
            gas_estimate: int | None = int(quote.gas_estimate)
        except ValueError:
            gas_estimate = None
        swaps = quote.route.swaps if quote.route else []
        pools = {swap.component_id for swap in swaps}
        protocols = sorted({swap.protocol for swap in swaps})
        return {
            "kind": kind,
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "pair": self.pair,
            "side": side,
            "size_numeraire": notional,
            "amount_in": quote.amount_in,
            "amount_out": quote.amount_out,
            "amount_out_net_gas": quote.amount_out_net_gas,
            "execution_price": price,
            "impact_pct": impact,
            "price_impact_bps": derive_price_impact_bps(price, self.robust_mid, side),
            # Fynd's own figure, passed through untouched. It is signed by trade
            # direction rather than by cost and is computed against whatever
            # reference the solver used, so it will not agree with the two
            # columns above and is not interchangeable with them.
            "price_impact_bps_raw": quote.price_impact_bps,
            "gas_estimate": gas_estimate,
            "gas_price": quote.gas_price,
            "gas_cost_token_out": gas_cost_token_out,
            "gas_cost_token_out_symbol": out_token.symbol,
            "solve_time_ms": solve_time_ms,
            "route_hash": route_hash(quote),
            "n_pools": len(pools),
            "protocols": protocols,
            "token_quality": self.token.quality,
            "token_tax": self.token.tax,
            "mixed_block": self.mixed_block,
        }

    def to_block_row(self) -> dict[str, Any]:
        """One summary record per block: identity plus the per-block derived
        values (`spot`, `robust_mid`, `median_depth`) and how the mid was won."""
        return {
            "pair": self.pair,
            "chain_id": self.chain_id,
            "token_symbol": self.token.symbol,
            "numeraire_symbol": self.numeraire.symbol,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "mixed_block": self.mixed_block,
            "spot": self.spot,
            "robust_mid": self.robust_mid,
            "median_depth": self.median_depth,
            "mid_source": self.mid_source,
            "gas_price_wei": self.gas_price_wei,
            "search_min": self.search_min,
            "search_max": self.search_max,
            "samples_per_side": self.samples_per_side,
            "mid_band_min": self.mid_band_min,
            "mid_band_max": self.mid_band_max,
            "numeraire_usd": self.numeraire_usd,
            "numeraire_usd_block": self.numeraire_usd_block,
            "numeraire_usd_spread_bps": self.numeraire_usd_spread_bps,
            "slippage": self.slippage,
            "duration_ms": self.duration_ms,
        }

    def to_rows(self) -> list[dict[str, Any]]:
        """Flatten to storage rows: `kind="curve"` sweep rungs plus
        `kind="anchor"` level rows. Off-block rungs are excluded."""
        rows: list[dict[str, Any]] = []
        sides: tuple[tuple[Side, list[SweepPoint]], ...] = (
            ("buy", self.curve_buy),
            ("sell", self.curve_sell),
        )
        for side, points in sides:
            for point in points:
                if not self._matches_block(point.quote):
                    continue
                rows.append(
                    self._row(
                        kind="curve",
                        side=side,
                        notional=point.notional,
                        price=point.price,
                        impact=point.impact_pct,
                        quote=point.quote,
                        solve_time_ms=point.solve_time_ms,
                    )
                )
        for level in self.levels:
            if not self._matches_block(level.quote):
                continue
            row = self._row(
                kind="anchor",
                side=level.side,
                notional=level.notional,
                price=level.price,
                impact=level.actual_impact_pct,
                quote=level.quote,
                solve_time_ms=level.solve_time_ms,
            )
            row.update(
                {
                    "target_impact_pct": level.target_impact_pct,
                    "bound": level.bound,
                    "target_reached": level.target_reached,
                    "derived_from": level.derived_from,
                }
            )
            rows.append(row)
        return rows

    def to_anchor_rows(self, *, sender: str | None) -> list[dict[str, Any]]:
        """Executable proof for the anchored headline levels: the transaction
        Fynd encoded, its fee breakdown, and a Tenderly simulation link.

        Only anchored levels are encoded, so only they appear here. These live
        in their own file rather than in `to_rows()` because calldata runs to
        kilobytes per level and the rows schema stays flat and cheap to page.
        """
        rows: list[dict[str, Any]] = []
        for level in self.levels:
            if level.derived_from != "anchored_bisection" or not self._matches_block(level.quote):
                continue
            transaction = level.quote.transaction
            fees = level.quote.fee_breakdown
            tenderly_url, tenderly_status = build_tenderly_url(
                sender=sender,
                transaction=transaction,
                chain_id=self.chain_id,
                block_number=self.block_number,
            )
            rows.append(
                {
                    "chain_id": self.chain_id,
                    "block_number": self.block_number,
                    "block_hash": self.block_hash,
                    "block_timestamp": self.block_timestamp,
                    "pair": self.pair,
                    "side": level.side,
                    "target_impact_pct": level.target_impact_pct,
                    "size_numeraire": level.notional,
                    "order_id": level.quote.order_id,
                    "solve_time_ms": level.solve_time_ms,
                    "transaction_to": transaction.to if transaction else None,
                    "transaction_value": transaction.value if transaction else None,
                    "transaction_data": transaction.data if transaction else None,
                    "router_fee": fees.router_fee if fees else None,
                    "client_fee": fees.client_fee if fees else None,
                    "max_slippage": fees.max_slippage if fees else None,
                    "min_amount_received": fees.min_amount_received if fees else None,
                    "tenderly_url": tenderly_url,
                    "tenderly_status": tenderly_status,
                }
            )
        return rows


def _mid_at_depth(
    fynd: FyndClient, config: SnapshotConfig, depth: float, spot: float
) -> float | None:
    buy = quote_at_notional(
        fynd,
        side="buy",
        notional=depth,
        amount=sized_amount(
            depth,
            side="buy",
            spot=spot,
            token_decimals=config.token.decimals,
            numeraire_decimals=config.numeraire.decimals,
        ),
        spot=spot,
        token=config.token,
        numeraire=config.numeraire,
        min_responses=1,
        timeout_ms=config.timeout_ms,
        encoding=False,
    )
    sell = quote_at_notional(
        fynd,
        side="sell",
        notional=depth,
        amount=sized_amount(
            depth,
            side="sell",
            spot=spot,
            token_decimals=config.token.decimals,
            numeraire_decimals=config.numeraire.decimals,
        ),
        spot=spot,
        token=config.token,
        numeraire=config.numeraire,
        min_responses=1,
        timeout_ms=config.timeout_ms,
        encoding=False,
    )
    if buy is None or sell is None:
        return None
    return (buy[1] + sell[1]) / 2.0


def probe_fallback_mid(
    fynd: FyndClient, config: SnapshotConfig, spot: float, max_depth: float
) -> tuple[float, float] | None:
    """Dedicated two-sided probes across the band when the sweep produced no
    usable mid pairs. Probes sort by depth for a deterministic band order."""
    depths = robust_mid_probe_depths(
        max_depth, band_min=config.mid_band_min, band_max=config.mid_band_max
    )
    depth_mid_pairs: list[tuple[float, float]] = []
    with ThreadPoolExecutor(max_workers=min(config.max_workers, len(depths))) as pool:
        futures = {pool.submit(_mid_at_depth, fynd, config, depth, spot): depth for depth in depths}
        for future in as_completed(futures):
            mid = future.result()
            if mid is not None:
                depth_mid_pairs.append((futures[future], mid))
    depth_mid_pairs.sort(key=lambda pair: pair[0])
    return choose_robust_mid(
        depth_mid_pairs, band_min=config.mid_band_min, band_max=config.mid_band_max
    )


def collect_snapshot(fynd: FyndClient, config: SnapshotConfig) -> Snapshot:
    """Measure one full snapshot: spot probe, two-sided sweep, majority-block
    reconciliation, robust mid, then per-target levels with anchored bisection.

    The mid settles before any level is derived because it is the reference every
    published impact is measured against, including the target an anchor bisects
    toward.

    Raises `SpotProbeError` when the spot probe fails — without spot nothing can
    be sized. Every other failure degrades (skipped rungs, fallback mid).
    """
    started = time.monotonic()
    spot = spot_price(
        fynd,
        token=config.token.address,
        token_decimals=config.token.decimals,
        numeraire=config.numeraire.address,
        numeraire_decimals=config.numeraire.decimals,
        probe_notional=config.probe_notional,
    )

    grid = numeraire_grid(config.search_min, config.search_max, config.samples_per_side)
    rungs = size_rungs(
        grid,
        spot=spot,
        token_decimals=config.token.decimals,
        numeraire_decimals=config.numeraire.decimals,
    )

    with ThreadPoolExecutor(max_workers=2) as sides_pool:
        futures_by_side = {
            side: sides_pool.submit(
                sweep_side,
                fynd,
                side=side,
                rungs=rungs,
                spot=spot,
                token=config.token,
                numeraire=config.numeraire,
                max_workers=config.max_workers,
                timeout_ms=config.timeout_ms,
            )
            for side in ("buy", "sell")
        }
        measured_buy: list[MeasuredRung] = futures_by_side["buy"].result()
        measured_sell: list[MeasuredRung] = futures_by_side["sell"].result()

    # Block identity is the majority block across the sweep, and the sweep alone.
    # The sweep is the measurement; anchors refine a handful of its rungs and are
    # issued strictly after it, so letting them vote would let a later block
    # relabel a snapshot the sweep had already settled. It also has to be settled
    # here, before any level is derived: the robust mid comes from the rungs on
    # this block, and every impact in the snapshot — swept rungs, anchor targets,
    # published basis points — is measured against that one number.
    measured_quotes = [rung.quote for rung in measured_buy + measured_sell]
    block_counts: dict[int, int] = {}
    for quote in measured_quotes:
        number = parse_quote_block(quote)
        if number is not None:
            block_counts[number] = block_counts.get(number, 0) + 1
    block_number: int | None = None
    if block_counts:
        block_number = max(block_counts, key=lambda number: block_counts[number])

    def matches_block(quote: OrderQuote) -> bool:
        number = parse_quote_block(quote)
        return number is None or block_number is None or number == block_number

    matching_buy = [rung for rung in measured_buy if matches_block(rung.quote)]
    matching_sell = [rung for rung in measured_sell if matches_block(rung.quote)]
    robust = robust_mid_from_sides(
        [(rung.notional, rung.price) for rung in matching_buy],
        [(rung.notional, rung.price) for rung in matching_sell],
        band_min=config.mid_band_min,
        band_max=config.mid_band_max,
    )
    mid_source: MidSource = "sweep_band"
    if robust is None:
        logger.warning(
            "robust mid for %s: no usable sweep pairs (%d buy / %d sell rungs kept); "
            "falling back to dedicated probes",
            config.pair,
            len(matching_buy),
            len(matching_sell),
        )
        max_depth = matching_buy[-1].notional if matching_buy else config.mid_band_max
        robust = probe_fallback_mid(fynd, config, spot, max_depth)
        mid_source = "probe_fallback"
    if robust is None:
        # Without a two-sided reference the only number left is the one-directional
        # spot probe, which charges the sell side the full spread as impact.
        # `mid_source` is how a reader tells that snapshot from a sound one.
        logger.warning("robust mid for %s: every mid probe failed; degrading to spot", config.pair)
        robust = (spot, config.mid_band_min)
        mid_source = "spot_degraded"
    robust_mid, median_depth = robust

    curve_buy = reference_sweep(measured_buy, side="buy", reference=robust_mid)
    curve_sell = reference_sweep(measured_sell, side="sell", reference=robust_mid)

    sides: tuple[tuple[Side, list[SweepPoint]], ...] = (("buy", curve_buy), ("sell", curve_sell))
    levels: list[Level] = []
    for target in config.impact_levels:
        for side, sweep in sides:
            level = derive_level_from_sweep(sweep, side=side, target_pct=target)
            if level is not None:
                levels.append(level)

    with ThreadPoolExecutor(max_workers=config.max_workers) as anchor_pool:
        anchor_futures: dict[tuple[float, Side], Any] = {
            (target, side): anchor_pool.submit(
                anchor_target_from_sweep,
                fynd,
                side=side,
                target_pct=target,
                sweep=sweep,
                spot=spot,
                impact_reference=robust_mid,
                token=config.token,
                numeraire=config.numeraire,
                timeout_ms=config.timeout_ms,
                max_iters=config.anchor_max_iters,
                tolerance=config.anchor_bisect_tolerance,
                slippage=config.slippage,
            )
            for target in config.anchor_targets
            for side, sweep in sides
        }
        for (target, side), future in anchor_futures.items():
            anchor: AnchorResult | None = future.result()
            if anchor is None:
                continue
            for index, level in enumerate(levels):
                if level.side == side and level.target_impact_pct == target:
                    levels[index] = level_from_anchor(
                        anchor,
                        side=side,
                        target_pct=target,
                        tolerance=config.anchor_accept_tolerance,
                    )
                    break

    # Anchors do not decide the label, but they are still evidence about it: a
    # level solved against another block means the snapshot spans more than one,
    # and `to_rows` drops that level rather than publishing it under this block.
    observed_blocks = set(block_counts)
    for level in levels:
        number = parse_quote_block(level.quote)
        if number is not None:
            observed_blocks.add(number)
    mixed_block = len(observed_blocks) > 1
    if mixed_block:
        logger.warning(
            "mixed-block snapshot for %s: swept %s, anchors reached %s -> labelled %s",
            config.pair,
            block_counts,
            sorted(observed_blocks - set(block_counts)),
            block_number,
        )

    all_quotes = measured_quotes + [level.quote for level in levels]

    block_hash: str | None = None
    block_timestamp: int | None = None
    gas_price_wei: str | None = None
    for quote in all_quotes:
        if not matches_block(quote):
            continue
        if block_hash is None and quote.block.hash:
            block_hash = quote.block.hash
        if block_timestamp is None and quote.block.timestamp:
            block_timestamp = quote.block.timestamp
        if gas_price_wei is None and quote.gas_price:
            gas_price_wei = quote.gas_price
        if block_hash and block_timestamp and gas_price_wei:
            break

    return Snapshot(
        pair=config.pair,
        chain_id=config.chain_id,
        token=config.token,
        numeraire=config.numeraire,
        block_number=block_number,
        block_hash=block_hash,
        block_timestamp=block_timestamp,
        gas_price_wei=gas_price_wei,
        mixed_block=mixed_block,
        spot=spot,
        robust_mid=robust_mid,
        median_depth=median_depth,
        mid_source=mid_source,
        curve_buy=curve_buy,
        curve_sell=curve_sell,
        levels=levels,
        search_min=config.search_min,
        search_max=config.search_max,
        samples_per_side=config.samples_per_side,
        mid_band_min=config.mid_band_min,
        mid_band_max=config.mid_band_max,
        numeraire_usd=config.numeraire_usd,
        numeraire_usd_block=config.numeraire_usd_block,
        numeraire_usd_spread_bps=config.numeraire_usd_spread_bps,
        slippage=config.slippage,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
