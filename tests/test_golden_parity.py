"""Golden method-parity gate.

`tests/golden/reference_expected.json` was produced by running the reference
marketprice.xyz collector (eth-price-poc-sdk checkout) against the exact AMM
simulator in `tests/amm_sim.py`. Our clean-room implementation must reproduce
the reference method's spot, robust_mid, median_depth, curve, levels, derived
price-impact bps, and route metadata bit-for-bit.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import amm_sim
from price_of_ethereum import FyndClient, SnapshotConfig, TokenMeta, collect_snapshot
from price_of_ethereum.pricing import derive_price_impact_bps
from price_of_ethereum.snapshot import Snapshot
from price_of_ethereum.sweep import Level

FIXTURE = json.loads((Path(__file__).parent / "golden" / "reference_expected.json").read_text())

WETH = TokenMeta(address=amm_sim.WETH_ADDRESS, symbol="WETH", decimals=18, quality=100, tax=0)
USDC = TokenMeta(address=amm_sim.USDC_ADDRESS, symbol="USDC", decimals=6, quality=100, tax=0)


def amm_client() -> FyndClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        order = payload["orders"][0]
        return httpx.Response(200, json=amm_sim.quote_response(order["token_in"], order["amount"]))

    return FyndClient(transport=httpx.MockTransport(handler))


@pytest.fixture(scope="module")
def snapshot() -> Snapshot:
    config = SnapshotConfig(
        token=WETH,
        numeraire=USDC,
        pair="ETH/USDC",
        chain_id=1,
        samples_per_side=FIXTURE["config"]["samples_per_side"],
        impact_levels=tuple(FIXTURE["config"]["impact_levels"]),
        max_workers=FIXTURE["config"]["max_workers"],
    )
    with amm_client() as fynd:
        return collect_snapshot(fynd, config)


def levels_by_key(snapshot: Snapshot) -> dict[tuple[str, float], Level]:
    return {(level.side, level.target_impact_pct): level for level in snapshot.levels}


def test_spot_matches_reference(snapshot: Snapshot) -> None:
    assert round(snapshot.spot, 6) == FIXTURE["spot_price"]


def test_robust_mid_matches_reference(snapshot: Snapshot) -> None:
    assert snapshot.robust_mid == FIXTURE["robust_mid"]
    assert snapshot.median_depth == FIXTURE["median_depth"]
    assert snapshot.mid_source == "sweep_band"


def test_block_identity_matches_reference(snapshot: Snapshot) -> None:
    assert snapshot.block_number == FIXTURE["block_number"]
    assert snapshot.block_hash == FIXTURE["block_hash"]
    assert snapshot.block_timestamp == FIXTURE["block_timestamp"]
    assert snapshot.gas_price_wei == FIXTURE["gas_price_wei"]
    assert snapshot.mixed_block == FIXTURE["mixed_block"]


def test_curve_matches_reference(snapshot: Snapshot) -> None:
    for side, points in (("buy", snapshot.curve_buy), ("sell", snapshot.curve_sell)):
        ours = [(point.notional, point.price, point.impact_pct) for point in points]
        expected = [
            (entry["amount_usd"], entry["price"], entry["impact_pct"])
            for entry in FIXTURE["curve"][side]
        ]
        assert ours == expected, f"curve {side} diverges from reference"


def test_levels_match_reference(snapshot: Snapshot) -> None:
    ours = levels_by_key(snapshot)
    assert len(ours) == len(FIXTURE["levels"])
    for expected in FIXTURE["levels"]:
        level = ours[(expected["side"], expected["target_impact_pct"])]
        assert level.actual_impact_pct == expected["actual_impact_pct"]
        assert level.notional == expected["amount_usd"]
        assert level.price == expected["effective_price"]
        expected_bound = None if expected["bound"] == "none" else expected["bound"]
        assert level.bound == expected_bound
        assert level.target_reached == expected["target_reached"]
        assert level.derived_from == expected["derived_from"]


def test_level_rows_derive_reference_bps_and_gas(snapshot: Snapshot) -> None:
    anchor_rows = {
        (row["side"], row["target_impact_pct"]): row
        for row in snapshot.to_rows()
        if row["kind"] == "anchor"
    }
    for expected in FIXTURE["levels"]:
        row = anchor_rows[(expected["side"], expected["target_impact_pct"])]
        assert row["price_impact_bps"] == expected["price_impact_bps"]
        assert row["gas_cost_token_out"] == expected["gas_cost_token_out"]


def test_level_routes_match_reference(snapshot: Snapshot) -> None:
    ours = levels_by_key(snapshot)
    for key, expected in FIXTURE["level_routes"].items():
        target_text, side = key.split("|")
        level = ours[(side, float(target_text))]
        assert level.quote.route is not None
        swaps = level.quote.route.swaps
        assert sorted({swap.protocol for swap in swaps}) == expected["protocols"]
        assert len({swap.component_id for swap in swaps}) == expected["pool_count"]
        assert len(swaps) == expected["hop_count"]


def test_bps_of_mid_is_zero(snapshot: Snapshot) -> None:
    assert (
        derive_price_impact_bps(snapshot.robust_mid, snapshot.robust_mid)
        == FIXTURE["sanity_bps_of_mid_is_zero"]
    )
