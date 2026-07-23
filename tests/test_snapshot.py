"""Unit tests for snapshot assembly: majority-block reconciliation, robust-mid
fallbacks, and row emission."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

import amm_sim
from price_of_ethereum import FyndClient, SnapshotConfig, TokenMeta, collect_snapshot
from price_of_ethereum.pricing import ROBUST_MID_MIN_DEPTH, robust_mid_probe_depths
from price_of_ethereum.sizing import atomic
from price_of_ethereum.snapshot import FALLBACK_PROBE_MAX_DEPTH

WETH = TokenMeta(address=amm_sim.WETH_ADDRESS, symbol="WETH", decimals=18, quality=100, tax=0)
USDC = TokenMeta(address=amm_sim.USDC_ADDRESS, symbol="USDC", decimals=6, quality=100, tax=0)


def make_config(**overrides: Any) -> SnapshotConfig:
    defaults: dict[str, Any] = {
        "token": WETH,
        "numeraire": USDC,
        "pair": "ETH/USDC",
        "chain_id": 1,
        "samples_per_side": 8,
        "impact_levels": (1.0,),
        "anchor_targets": (),
        "max_workers": 2,
    }
    defaults.update(overrides)
    return SnapshotConfig(**defaults)


def client_with(mutate) -> FyndClient:
    """AMM-backed client; `mutate(order, body)` can rewrite each response."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        order = payload["orders"][0]
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        mutate(order, body)
        return httpx.Response(200, json=body)

    return FyndClient(transport=httpx.MockTransport(handler))


# Sell-side inputs above this many WETH atomic units get rewritten in the
# mixed-block tests: ~$1M notional at the AMM's 2500 spot.
LARGE_SELL_THRESHOLD = 400 * 10**18


def test_majority_block_excludes_minority_rungs() -> None:
    def mutate(order: dict, body: dict) -> None:
        is_sell = order["token_in"].lower() == amm_sim.WETH_ADDRESS.lower()
        if is_sell and int(order["amount"]) > LARGE_SELL_THRESHOLD:
            body["orders"][0]["block"]["number"] = amm_sim.BLOCK_NUMBER + 1

    with client_with(mutate) as fynd:
        snapshot = collect_snapshot(fynd, make_config())

    assert snapshot.block_number == amm_sim.BLOCK_NUMBER
    assert snapshot.mixed_block is True
    rows = snapshot.to_rows()
    sell_curve_notionals = {
        row["size_numeraire"] for row in rows if row["kind"] == "curve" and row["side"] == "sell"
    }
    buy_curve_notionals = {
        row["size_numeraire"] for row in rows if row["kind"] == "curve" and row["side"] == "buy"
    }
    assert len(snapshot.curve_sell) == 8  # measured rungs are kept on the snapshot
    assert len(sell_curve_notionals) == 6  # ...but off-block rungs never become rows
    assert len(buy_curve_notionals) == 8
    assert all(row["mixed_block"] is True for row in rows)


def test_null_block_rungs_are_kept() -> None:
    def mutate(order: dict, body: dict) -> None:
        is_sell = order["token_in"].lower() == amm_sim.WETH_ADDRESS.lower()
        if is_sell and int(order["amount"]) > LARGE_SELL_THRESHOLD:
            body["orders"][0]["block"]["number"] = 0

    with client_with(mutate) as fynd:
        snapshot = collect_snapshot(fynd, make_config())

    assert snapshot.block_number == amm_sim.BLOCK_NUMBER
    assert snapshot.mixed_block is False
    sell_rows = [
        row for row in snapshot.to_rows() if row["kind"] == "curve" and row["side"] == "sell"
    ]
    assert len(sell_rows) == 8


def test_probe_fallback_mid_when_sweep_fails() -> None:
    spot_amount = atomic(1000.0, USDC.decimals)
    probe_quote = amm_sim.order_quote(amm_sim.USDC_ADDRESS, str(spot_amount))
    spot = 1000.0 / (int(probe_quote["amount_out"]) / 10**18)
    depths = robust_mid_probe_depths(FALLBACK_PROBE_MAX_DEPTH)
    allowed_buy = {spot_amount} | {atomic(depth, USDC.decimals) for depth in depths}
    allowed_sell = {atomic(depth / spot, WETH.decimals) for depth in depths}

    def mutate(order: dict, body: dict) -> None:
        is_buy = order["token_in"].lower() == amm_sim.USDC_ADDRESS.lower()
        allowed = allowed_buy if is_buy else allowed_sell
        if int(order["amount"]) not in allowed:
            body["orders"][0]["status"] = "no_route_found"

    with client_with(mutate) as fynd:
        snapshot = collect_snapshot(fynd, make_config(samples_per_side=4))

    assert snapshot.mid_source == "probe_fallback"
    assert snapshot.curve_buy == [] and snapshot.curve_sell == []
    assert snapshot.block_number is None  # block identity comes from kept quotes only
    assert snapshot.to_rows() == []
    assert abs(snapshot.robust_mid / spot - 1) < 0.01
    assert ROBUST_MID_MIN_DEPTH <= snapshot.median_depth <= 10_000.0


def test_spot_degraded_mid_when_everything_fails() -> None:
    spot_amount = atomic(1000.0, USDC.decimals)

    def mutate(order: dict, body: dict) -> None:
        if int(order["amount"]) != spot_amount:
            body["orders"][0]["status"] = "no_route_found"

    with client_with(mutate) as fynd:
        snapshot = collect_snapshot(fynd, make_config(samples_per_side=4))

    assert snapshot.mid_source == "spot_degraded"
    assert snapshot.robust_mid == snapshot.spot
    assert snapshot.median_depth == ROBUST_MID_MIN_DEPTH


def test_orphan_anchor_target_rejected_at_config_time() -> None:
    with pytest.raises(ValueError, match="anchor_targets"):
        make_config(impact_levels=(1.0,), anchor_targets=(5.0,))


def test_malformed_gas_fields_null_gas_columns_but_keep_row() -> None:
    def mutate(order: dict, body: dict) -> None:
        body["orders"][0]["amount_out_net_gas"] = "not-a-number"
        body["orders"][0]["gas_estimate"] = "also-not-a-number"

    with client_with(mutate) as fynd:
        snapshot = collect_snapshot(fynd, make_config(samples_per_side=4))

    rows = snapshot.to_rows()
    assert len(rows) > 0
    for row in rows:
        assert row["gas_estimate"] is None
        assert row["gas_cost_token_out"] is None
        assert row["execution_price"] > 0  # pricing unaffected by gas fields


def test_rows_carry_the_full_data_model() -> None:
    with client_with(lambda order, body: None) as fynd:
        snapshot = collect_snapshot(fynd, make_config(anchor_targets=(1.0,)))

    rows = snapshot.to_rows()
    curve_rows = [row for row in rows if row["kind"] == "curve"]
    anchor_rows = [row for row in rows if row["kind"] == "anchor"]
    assert len(curve_rows) == 16
    assert len(anchor_rows) == 2

    expected_route_hash = hashlib.sha256(amm_sim.POOL_COMPONENT_ID.encode()).hexdigest()[:16]
    for row in rows:
        assert row["chain_id"] == 1
        assert row["pair"] == "ETH/USDC"
        assert row["block_number"] == amm_sim.BLOCK_NUMBER
        assert row["block_hash"] == amm_sim.BLOCK_HASH
        assert row["block_timestamp"] == amm_sim.BLOCK_TIMESTAMP
        assert row["gas_price"] == str(amm_sim.GAS_PRICE_WEI)
        assert row["gas_estimate"] == amm_sim.GAS_ESTIMATE
        assert row["price_impact_bps_raw"] is None
        assert row["route_hash"] == expected_route_hash
        assert row["n_pools"] == 1
        assert row["protocols"] == [amm_sim.POOL_PROTOCOL]
        assert row["token_quality"] == 100
        assert row["token_tax"] == 0
        assert row["mixed_block"] is False
        out_decimals = WETH.decimals if row["side"] == "buy" else USDC.decimals
        assert row["gas_cost_token_out"] == amm_sim.NET_GAS_DISCOUNT / 10**out_decimals
        assert int(row["amount_out"]) - int(row["amount_out_net_gas"]) == amm_sim.NET_GAS_DISCOUNT

    for row in anchor_rows:
        assert row["target_impact_pct"] == 1.0
        assert row["derived_from"] in ("nearest_real_quote", "anchored_bisection")
        assert "bound" in row and "target_reached" in row
