"""Sizing tests: pure grid/atomic math, plus spot probe over a mocked Fynd."""

from __future__ import annotations

import math

import httpx
import pytest

from price_of_ethereum.fynd import FyndClient
from price_of_ethereum.sizing import (
    SpotProbeError,
    atomic,
    numeraire_grid,
    size_rungs,
    spot_price,
)

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


def test_atomic_truncates() -> None:
    assert atomic(1000.0, 6) == 1_000_000_000
    assert atomic(1.5, 18) == 1_500_000_000_000_000_000


def test_atomic_exact_at_large_scale() -> None:
    # Above 2**53 a plain float multiply would drift; Decimal keeps it exact.
    assert atomic(50_000_000.0, 18) == 50_000_000 * 10**18


def test_numeraire_grid_endpoints_and_log_spacing() -> None:
    grid = numeraire_grid(100.0, 50_000_000.0, 100)
    assert len(grid) == 100
    assert grid[0] == pytest.approx(100.0)
    assert grid[-1] == pytest.approx(50_000_000.0)
    ratios = [grid[i + 1] / grid[i] for i in range(len(grid) - 1)]
    assert max(ratios) == pytest.approx(min(ratios))  # constant ratio == log-spaced


def test_numeraire_grid_single_sample() -> None:
    assert numeraire_grid(100.0, 50_000_000.0, 1) == [100.0]


def test_numeraire_grid_min_equals_max() -> None:
    grid = numeraire_grid(1000.0, 1000.0, 5)
    assert grid == [1000.0] * 5


def test_numeraire_grid_rejects_zero_samples() -> None:
    with pytest.raises(ValueError):
        numeraire_grid(100.0, 1000.0, 0)


def test_size_rungs_both_sides_matched_notional() -> None:
    # spot = 2500 USDC per WETH; USDC 6 decimals, WETH 18 decimals.
    grid = [1000.0, 5000.0]
    rungs = size_rungs(grid, spot=2500.0, token_decimals=18, numeraire_decimals=6)
    assert rungs[0].notional == 1000.0
    assert rungs[0].buy_amount == 1_000 * 10**6  # 1000 USDC
    assert rungs[0].sell_amount == atomic(1000.0 / 2500.0, 18)  # 0.4 WETH
    assert rungs[1].buy_amount == 5_000 * 10**6


def test_size_rungs_rejects_nonpositive_spot() -> None:
    with pytest.raises(ValueError):
        size_rungs([1000.0], spot=0.0, token_decimals=18, numeraire_decimals=6)


def test_size_rungs_zero_truncation_raises() -> None:
    # A 0-decimal token at a small notional/high spot truncates the sell side to 0.
    with pytest.raises(ValueError, match="zero base-unit amount"):
        size_rungs([100.0], spot=2500.0, token_decimals=0, numeraire_decimals=6)


def _fynd_returning(amount_out: str, status: str = "success") -> FyndClient:
    quote = {
        "orders": [
            {
                "order_id": "x",
                "status": status,
                "amount_in": "1000000000",
                "amount_out": amount_out,
                "amount_out_net_gas": amount_out,
                "gas_estimate": "150000",
                "block": {"number": 21000000, "hash": "0xabc", "timestamp": 1730000000},
            }
        ],
        "total_gas_estimate": "150000",
        "solve_time_ms": 12,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=quote)

    return FyndClient(transport=httpx.MockTransport(handler))


def test_spot_price_from_probe() -> None:
    # Buy WETH with 1000 USDC, receive 0.4 WETH -> spot 2500 USDC/WETH.
    fynd = _fynd_returning(str(4 * 10**17))
    spot = spot_price(
        fynd,
        token=WETH,
        token_decimals=18,
        numeraire=USDC,
        numeraire_decimals=6,
    )
    assert spot == pytest.approx(2500.0)


def test_spot_price_probe_failure_raises() -> None:
    fynd = _fynd_returning("0", status="no_route_found")
    with pytest.raises(SpotProbeError):
        spot_price(fynd, token=WETH, token_decimals=18, numeraire=USDC, numeraire_decimals=6)


def test_spot_price_zero_output_raises() -> None:
    fynd = _fynd_returning("0")
    with pytest.raises(SpotProbeError):
        spot_price(fynd, token=WETH, token_decimals=18, numeraire=USDC, numeraire_decimals=6)


def test_spot_price_malformed_amount_raises_spot_probe_error() -> None:
    fynd = _fynd_returning("not-a-number")
    with pytest.raises(SpotProbeError, match="malformed amount_out"):
        spot_price(fynd, token=WETH, token_decimals=18, numeraire=USDC, numeraire_decimals=6)


def test_spot_price_transport_failure_raises_spot_probe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    fynd = FyndClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SpotProbeError, match="request failed"):
        spot_price(fynd, token=WETH, token_decimals=18, numeraire=USDC, numeraire_decimals=6)


def test_spot_price_empty_orders_raises() -> None:
    empty = {"orders": [], "total_gas_estimate": "0", "solve_time_ms": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=empty)

    fynd = FyndClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SpotProbeError):
        spot_price(fynd, token=WETH, token_decimals=18, numeraire=USDC, numeraire_decimals=6)


def test_full_eth_usdc_sizing_range() -> None:
    # End-to-end sizing sanity for ETH/USDC over $100..$50M at spot 2500.
    grid = numeraire_grid(100.0, 50_000_000.0, 100)
    rungs = size_rungs(grid, spot=2500.0, token_decimals=18, numeraire_decimals=6)
    # Endpoints are exp(log(x)) — a few ULP off the exact bounds, matching the
    # reference collector's grid — so compare at 1e-9 relative, not exactly.
    assert math.isclose(rungs[0].buy_amount, 100 * 10**6, rel_tol=1e-9)
    assert math.isclose(rungs[-1].buy_amount, atomic(50_000_000.0, 6), rel_tol=1e-9)
    # sell side: notional/spot WETH, e.g. $50M / 2500 = 20000 WETH at the top.
    assert math.isclose(rungs[-1].sell_amount / 10**18, 20_000.0, rel_tol=1e-9)
