"""Unit tests for the sweep fan-out, anchored bisection, and level derivation."""

from __future__ import annotations

import itertools
import json

import httpx

import amm_sim
from price_of_ethereum import FyndClient, TokenMeta
from price_of_ethereum.fynd.models import OrderQuote
from price_of_ethereum.sizing import numeraire_grid, size_rungs
from price_of_ethereum.sweep import (
    AnchorResult,
    SweepPoint,
    anchor_target_from_sweep,
    derive_level_from_sweep,
    level_from_anchor,
    sweep_side,
)

WETH = TokenMeta(address=amm_sim.WETH_ADDRESS, symbol="WETH", decimals=18, quality=100, tax=0)
USDC = TokenMeta(address=amm_sim.USDC_ADDRESS, symbol="USDC", decimals=6, quality=100, tax=0)

SPOT = 2500.0


def recording_client(
    fail_amounts: frozenset[int] = frozenset(),
) -> tuple[FyndClient, list[dict]]:
    """AMM-backed mock client that logs every request payload. Amounts in
    `fail_amounts` return a non-success status."""
    request_log: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_log.append(payload)
        order = payload["orders"][0]
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        if int(order["amount"]) in fail_amounts:
            body["orders"][0]["status"] = "no_route_found"
        return httpx.Response(200, json=body)

    return FyndClient(transport=httpx.MockTransport(handler)), request_log


def make_point(notional: float, impact: float) -> SweepPoint:
    quote = OrderQuote.model_validate(amm_sim.order_quote(amm_sim.USDC_ADDRESS, str(10**9)))
    return SweepPoint(
        notional=notional,
        price=SPOT * (1 + impact / 100.0),
        impact_pct=impact,
        quote=quote,
        solve_time_ms=amm_sim.SOLVE_TIME_MS,
    )


class TestSweepSide:
    def test_sorted_ascending_and_failures_skipped(self) -> None:
        grid = numeraire_grid(50.0, 50_000.0, 5)
        rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
        fynd, request_log = recording_client(fail_amounts=frozenset({rungs[2].buy_amount}))
        points = sweep_side(
            fynd, side="buy", rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=2
        )
        assert [point.notional for point in points] == sorted(
            round(rung.notional, 2) for rung in rungs if rung is not rungs[2]
        )
        assert all(payload["options"]["min_responses"] == 1 for payload in request_log)

    def test_bulk_sweep_never_asks_for_encoding(self) -> None:
        # Nothing downstream reads calldata off a sweep rung, and encoding it
        # costs Fynd real work on every one of ~200 quotes per block.
        grid = numeraire_grid(50.0, 50_000.0, 5)
        rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
        fynd, request_log = recording_client()
        sweep_side(
            fynd, side="buy", rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=2
        )
        assert request_log
        assert all("encoding_options" not in payload["options"] for payload in request_log)

    def test_quotes_the_amounts_the_rungs_were_sized_to(self) -> None:
        # The rung owns the sizing; the sweep must not re-derive it.
        grid = numeraire_grid(50.0, 50_000.0, 5)
        rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
        for side, expected in (
            ("buy", [rung.buy_amount for rung in rungs]),
            ("sell", [rung.sell_amount for rung in rungs]),
        ):
            fynd, request_log = recording_client()
            sweep_side(
                fynd, side=side, rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=2
            )
            sent = sorted(int(payload["orders"][0]["amount"]) for payload in request_log)
            assert sent == sorted(expected)

    def test_malformed_amount_out_skips_rung(self) -> None:
        grid = numeraire_grid(50.0, 50_000.0, 4)
        rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
        corrupted_amount = rungs[1].buy_amount

        def handler(request: httpx.Request) -> httpx.Response:
            order = json.loads(request.content)["orders"][0]
            body = amm_sim.quote_response(order["token_in"], order["amount"])
            if int(order["amount"]) == corrupted_amount:
                body["orders"][0]["amount_out"] = "not-a-number"
            return httpx.Response(200, json=body)

        with FyndClient(transport=httpx.MockTransport(handler)) as fynd:
            points = sweep_side(
                fynd, side="buy", rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=2
            )
        assert len(points) == 3
        assert round(rungs[1].notional, 2) not in {point.notional for point in points}

    def test_invalid_response_body_skips_rung(self) -> None:
        grid = numeraire_grid(50.0, 50_000.0, 3)
        rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
        garbage_amount = rungs[0].buy_amount

        def handler(request: httpx.Request) -> httpx.Response:
            order = json.loads(request.content)["orders"][0]
            if int(order["amount"]) == garbage_amount:
                return httpx.Response(200, json={"unexpected": "shape"})
            return httpx.Response(
                200, json=amm_sim.quote_response(order["token_in"], order["amount"])
            )

        with FyndClient(transport=httpx.MockTransport(handler)) as fynd:
            points = sweep_side(
                fynd, side="buy", rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=2
            )
        assert len(points) == 2

    def test_sell_side_sizes_from_spot(self) -> None:
        grid = numeraire_grid(2500.0, 2500.0, 1)
        rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
        fynd, request_log = recording_client()
        points = sweep_side(
            fynd, side="sell", rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=1
        )
        assert len(points) == 1
        assert request_log[0]["orders"][0]["token_in"] == amm_sim.WETH_ADDRESS
        assert request_log[0]["orders"][0]["amount"] == str(10**18)  # $2500 / 2500 = 1 WETH


class TestAnchorTargetFromSweep:
    def test_no_bracket_returns_none(self) -> None:
        sweep = [make_point(100.0, 0.01), make_point(1000.0, 0.05)]
        fynd, request_log = recording_client()
        anchor = anchor_target_from_sweep(
            fynd, side="buy", target_pct=5.0, sweep=sweep, spot=SPOT, token=WETH, numeraire=USDC
        )
        assert anchor is None
        assert request_log == []

    def test_bisection_quotes_wait_for_all_pools(self) -> None:
        grid = numeraire_grid(50.0, 50_000_000.0, 15)
        rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
        fynd, request_log = recording_client()
        sweep = sweep_side(
            fynd, side="buy", rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=2
        )
        request_log.clear()
        anchor = anchor_target_from_sweep(
            fynd, side="buy", target_pct=5.0, sweep=sweep, spot=SPOT, token=WETH, numeraire=USDC
        )
        assert anchor is not None
        assert 0 < len(request_log) <= 3
        assert all(payload["options"]["min_responses"] == 0 for payload in request_log)
        # Anchors are the one place calldata is kept, so they encode.
        assert all("encoding_options" in payload["options"] for payload in request_log)
        low, high = None, None
        for first, second in itertools.pairwise(sweep):
            if first.impact_pct < 5.0 <= second.impact_pct:
                low, high = first.notional, second.notional
        assert low is not None and high is not None and low <= anchor.notional <= high


class TestDeriveLevelFromSweep:
    def test_empty_sweep_is_none(self) -> None:
        assert derive_level_from_sweep([], side="buy", target_pct=1.0) is None

    def test_crossing_takes_closer_endpoint(self) -> None:
        sweep = [make_point(100.0, 0.2), make_point(1000.0, 0.9), make_point(10_000.0, 4.0)]
        level = derive_level_from_sweep(sweep, side="buy", target_pct=1.0)
        assert level is not None
        assert level.bound is None
        assert level.target_reached is True
        assert level.notional == 1000.0  # 1.0 is closer to 0.9 than to 4.0
        assert level.derived_from == "nearest_real_quote"

    def test_capped_sweep_is_max_bound(self) -> None:
        sweep = [make_point(100.0, 0.2), make_point(1000.0, 0.9)]
        level = derive_level_from_sweep(sweep, side="buy", target_pct=5.0)
        assert level is not None
        assert level.bound == "max"
        assert level.target_reached is False
        assert level.notional == 1000.0

    def test_smallest_size_already_exceeding_is_min_bound(self) -> None:
        sweep = [make_point(100.0, 2.0), make_point(1000.0, 4.0)]
        level = derive_level_from_sweep(sweep, side="buy", target_pct=1.0)
        assert level is not None
        assert level.bound == "min"
        assert level.target_reached is False
        assert level.notional == 100.0

    def test_first_rung_dip_stays_min_bound(self) -> None:
        # When the smallest rung already exceeds the target, the level is a min
        # bound even though a later dip crosses back below it.
        sweep = [make_point(100.0, 2.0), make_point(1000.0, 0.5), make_point(10_000.0, 3.0)]
        level = derive_level_from_sweep(sweep, side="buy", target_pct=1.0)
        assert level is not None
        assert level.bound == "min"
        assert level.target_reached is False
        assert level.notional == 100.0

    def test_non_monotonic_dip_still_finds_first_crossing(self) -> None:
        # Route recomposition can dip impact as size grows; the scan must catch
        # the first sign change even after an earlier above-target point.
        sweep = [
            make_point(100.0, 0.5),
            make_point(1000.0, 1.5),
            make_point(10_000.0, 0.8),
            make_point(100_000.0, 3.0),
        ]
        level = derive_level_from_sweep(sweep, side="buy", target_pct=1.0)
        assert level is not None
        assert level.bound is None
        assert level.notional == 1000.0


class TestLevelFromAnchor:
    def anchor(self, impact: float) -> AnchorResult:
        quote = OrderQuote.model_validate(amm_sim.order_quote(amm_sim.USDC_ADDRESS, str(10**9)))
        return AnchorResult(
            notional=12345.6789,
            price=SPOT * (1 + impact / 100.0),
            impact_pct=impact,
            quote=quote,
            solve_time_ms=amm_sim.SOLVE_TIME_MS,
        )

    def test_within_tolerance_reaches_target(self) -> None:
        level = level_from_anchor(self.anchor(5.1), side="buy", target_pct=5.0, tolerance=0.05)
        assert level.bound is None
        assert level.target_reached is True
        assert level.notional == 12345.68
        assert level.derived_from == "anchored_bisection"

    def test_undershoot_is_max_bound(self) -> None:
        level = level_from_anchor(self.anchor(4.0), side="buy", target_pct=5.0, tolerance=0.05)
        assert level.bound == "max"
        assert level.target_reached is False

    def test_overshoot_is_min_bound(self) -> None:
        level = level_from_anchor(self.anchor(6.0), side="buy", target_pct=5.0, tolerance=0.05)
        assert level.bound == "min"
        assert level.target_reached is False


def encoding_refusing_client() -> tuple[FyndClient, list[dict]]:
    """Fynd that prices anything but refuses to encode, as it does at sizes
    where the default slippage fails its price guard."""
    request_log: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        request_log.append(payload)
        order = payload["orders"][0]
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        if "encoding_options" in payload["options"]:
            body["orders"][0]["status"] = "price_check_failed"
        return httpx.Response(200, json=body)

    return FyndClient(transport=httpx.MockTransport(handler)), request_log


def test_anchor_survives_a_size_fynd_will_not_encode() -> None:
    # Losing the level as well as the calldata would throw away a measurement
    # Fynd was willing to make, which is the part nobody can reconstruct later.
    grid = numeraire_grid(50.0, 50_000_000.0, 15)
    rungs = size_rungs(grid, spot=SPOT, token_decimals=18, numeraire_decimals=6)
    fynd, _ = recording_client()
    sweep = sweep_side(
        fynd, side="buy", rungs=rungs, spot=SPOT, token=WETH, numeraire=USDC, max_workers=2
    )

    refusing, request_log = encoding_refusing_client()
    anchor = anchor_target_from_sweep(
        refusing, side="buy", target_pct=5.0, sweep=sweep, spot=SPOT, token=WETH, numeraire=USDC
    )

    assert anchor is not None
    assert anchor.quote.transaction is None  # recorded as absent, not faked
    encoded = [p for p in request_log if "encoding_options" in p["options"]]
    unencoded = [p for p in request_log if "encoding_options" not in p["options"]]
    assert encoded, "the encoded attempt still comes first"
    assert unencoded, "and the retry drops encoding rather than the level"
