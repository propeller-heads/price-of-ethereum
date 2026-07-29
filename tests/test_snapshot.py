"""Unit tests for snapshot assembly: majority-block reconciliation, robust-mid
fallbacks, and row emission."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import amm_sim
from price_of_ethereum import FyndClient, SnapshotConfig, TokenMeta, collect_snapshot
from price_of_ethereum.fynd.client import DUMMY_SENDER
from price_of_ethereum.fynd.models import Transaction
from price_of_ethereum.pricing import ROBUST_MID_MIN_DEPTH, robust_mid_probe_depths
from price_of_ethereum.sizing import atomic
from price_of_ethereum.snapshot import build_tenderly_url

WETH = TokenMeta(address=amm_sim.WETH_ADDRESS, symbol="WETH", decimals=18, quality=100, tax=0)
USDC = TokenMeta(address=amm_sim.USDC_ADDRESS, symbol="USDC", decimals=6, quality=100, tax=0)


def make_config(**overrides: Any) -> SnapshotConfig:
    defaults: dict[str, Any] = {
        "token": WETH,
        "numeraire": USDC,
        "pair": "ETH/USDC",
        "chain_id": 1,
        "samples_per_side": 8,
        # The band is in numeraire units and only means dollars when the
        # numeraire is one, so both it and the rate that scaled it are recorded.
        "mid_band_min": 2_500.0,
        "mid_band_max": 10_000.0,
        "numeraire_usd": None,
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

ROUTER_ADDRESS = "0x" + "cd" * 20
CALLDATA = "0xdeadbeef"
# Stands in for a real address someone passed with --sender; only a sender that
# is not the quote-only placeholder earns a `ready` link.
FUNDED_SENDER = "0x" + "ab" * 20
FEE_BREAKDOWN = {
    "router_fee": "11",
    "client_fee": "22",
    "max_slippage": "33",
    "min_amount_received": "44",
}


def encoding_aware_client() -> FyndClient:
    """AMM-backed client that attaches a transaction only when the request
    actually asked Fynd to encode one — as a real Fynd does."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        order = payload["orders"][0]
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        if "encoding_options" in payload["options"]:
            body["orders"][0]["transaction"] = {
                "to": ROUTER_ADDRESS,
                "value": "0",
                "data": CALLDATA,
            }
            body["orders"][0]["fee_breakdown"] = dict(FEE_BREAKDOWN)
        return httpx.Response(200, json=body)

    return FyndClient(transport=httpx.MockTransport(handler))


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
    config = make_config(samples_per_side=4)
    spot_amount = atomic(1000.0, USDC.decimals)
    probe_quote = amm_sim.order_quote(amm_sim.USDC_ADDRESS, str(spot_amount))
    spot = 1000.0 / (int(probe_quote["amount_out"]) / 10**18)
    depths = robust_mid_probe_depths(
        config.mid_band_max, band_min=config.mid_band_min, band_max=config.mid_band_max
    )
    allowed_buy = {spot_amount} | {atomic(depth, USDC.decimals) for depth in depths}
    allowed_sell = {atomic(depth / spot, WETH.decimals) for depth in depths}

    def mutate(order: dict, body: dict) -> None:
        is_buy = order["token_in"].lower() == amm_sim.USDC_ADDRESS.lower()
        allowed = allowed_buy if is_buy else allowed_sell
        if int(order["amount"]) not in allowed:
            body["orders"][0]["status"] = "no_route_found"

    with client_with(mutate) as fynd:
        snapshot = collect_snapshot(fynd, config)

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


def test_to_block_row_carries_every_summary_field() -> None:
    with client_with(lambda order, body: None) as fynd:
        snapshot = collect_snapshot(fynd, make_config())

    assert snapshot.to_block_row() == {
        "pair": "ETH/USDC",
        "chain_id": 1,
        "token_symbol": "WETH",
        "numeraire_symbol": "USDC",
        "block_number": amm_sim.BLOCK_NUMBER,
        "block_hash": amm_sim.BLOCK_HASH,
        "block_timestamp": amm_sim.BLOCK_TIMESTAMP,
        "mixed_block": False,
        "spot": snapshot.spot,
        "robust_mid": snapshot.robust_mid,
        "median_depth": snapshot.median_depth,
        "mid_source": "sweep_band",
        "gas_price_wei": str(amm_sim.GAS_PRICE_WEI),
        "search_min": 50.0,
        "search_max": 50_000_000.0,
        "samples_per_side": 8,
        # The band is in numeraire units and only reads as dollars when the
        # numeraire is one, so it travels with the rate that scaled it.
        "mid_band_min": 2_500.0,
        "mid_band_max": 10_000.0,
        "numeraire_usd": None,
        # The rate is measured once per run, so the block it came from travels
        # with it and a reader can see how stale it is on a later block. Its
        # round-trip cost travels too, because a rate taken on a thin pair is
        # worth less trust than the same number taken on a deep one.
        "numeraire_usd_block": None,
        "numeraire_usd_spread": None,
        # The anchors' calldata is only meaningful against the bound it was
        # encoded for, so the summary records it.
        "slippage": "0.001",
        "duration_ms": snapshot.duration_ms,
    }


class TestBuildTenderlyUrl:
    transaction = Transaction(to=ROUTER_ADDRESS, value="0", data=CALLDATA)

    def test_ready_url_carries_every_simulator_parameter(self) -> None:
        url, status = build_tenderly_url(
            sender=FUNDED_SENDER, transaction=self.transaction, chain_id=1, block_number=12_345
        )
        assert status == "ready"
        assert url is not None
        query = parse_qs(urlparse(url).query)
        assert query == {
            "network": ["1"],
            "from": [FUNDED_SENDER],
            "contractAddress": [ROUTER_ADDRESS],
            "rawFunctionInput": [CALLDATA],
            "value": ["0"],
            "block": ["12345"],
        }

    def test_chain_id_selects_the_simulated_network(self) -> None:
        url, _ = build_tenderly_url(
            sender=DUMMY_SENDER, transaction=self.transaction, chain_id=8453, block_number=None
        )
        assert url is not None
        query = parse_qs(urlparse(url).query)
        assert query["network"] == ["8453"]
        assert "block" not in query

    def test_placeholder_sender_links_but_does_not_claim_ready(self) -> None:
        # The default sender holds no balance and has approved nothing, so the
        # link opens but reverts on the transfer. Saying `ready` would promise a
        # simulation that cannot succeed.
        url, status = build_tenderly_url(
            sender=DUMMY_SENDER, transaction=self.transaction, chain_id=1, block_number=None
        )
        assert status == "placeholder_sender"
        assert url is not None
        assert parse_qs(urlparse(url).query)["from"] == [DUMMY_SENDER]

    def test_missing_sender_is_named_not_nulled(self) -> None:
        assert build_tenderly_url(
            sender=None, transaction=self.transaction, chain_id=1, block_number=1
        ) == (None, "missing_sender")

    def test_unencoded_quote_is_no_transaction(self) -> None:
        assert build_tenderly_url(
            sender=DUMMY_SENDER, transaction=None, chain_id=1, block_number=1
        ) == (None, "no_transaction")

    def test_transaction_without_calldata_is_no_transaction(self) -> None:
        empty = Transaction(to=ROUTER_ADDRESS, value="0", data="")
        assert build_tenderly_url(
            sender=DUMMY_SENDER, transaction=empty, chain_id=1, block_number=1
        ) == (None, "no_transaction")


class TestAnchorRows:
    def snapshot_with_anchors(self):
        with encoding_aware_client() as fynd:
            return collect_snapshot(fynd, make_config(anchor_targets=(1.0,)))

    def test_anchor_rows_carry_the_executable_proof(self) -> None:
        rows = self.snapshot_with_anchors().to_anchor_rows(sender=FUNDED_SENDER)
        assert sorted(row["side"] for row in rows) == ["buy", "sell"]
        for row in rows:
            assert row["target_impact_pct"] == 1.0
            # Block identity and pair travel with the proof, so an anchors file
            # joins against the rows and blocks files on its own terms.
            assert row["chain_id"] == 1
            assert row["pair"] == "ETH/USDC"
            assert row["block_number"] == amm_sim.BLOCK_NUMBER
            assert row["block_hash"] == amm_sim.BLOCK_HASH
            assert row["block_timestamp"] == amm_sim.BLOCK_TIMESTAMP
            assert row["size_numeraire"] > 0
            assert row["order_id"] == "order-1"
            assert row["solve_time_ms"] == amm_sim.SOLVE_TIME_MS
            assert row["transaction_to"] == ROUTER_ADDRESS
            assert row["transaction_value"] == "0"
            assert row["transaction_data"] == CALLDATA
            assert {key: row[key] for key in FEE_BREAKDOWN} == FEE_BREAKDOWN
            assert row["tenderly_status"] == "ready"
            assert row["tenderly_url"].startswith("https://dashboard.tenderly.co/simulator/new?")

    def test_anchor_rows_report_a_missing_sender(self) -> None:
        rows = self.snapshot_with_anchors().to_anchor_rows(sender=None)
        assert rows
        assert all(row["tenderly_status"] == "missing_sender" for row in rows)
        assert all(row["tenderly_url"] is None for row in rows)
        # The proof itself survives; only the simulator link needs a sender.
        assert all(row["transaction_data"] == CALLDATA for row in rows)

    def test_sweep_derived_levels_produce_no_anchor_rows(self) -> None:
        with encoding_aware_client() as fynd:
            snapshot = collect_snapshot(fynd, make_config(anchor_targets=()))
        assert snapshot.levels
        assert snapshot.to_anchor_rows(sender=DUMMY_SENDER) == []

    def test_calldata_stays_out_of_the_rows_file(self) -> None:
        snapshot = self.snapshot_with_anchors()
        assert snapshot.to_anchor_rows(sender=DUMMY_SENDER)
        for row in snapshot.to_rows():
            assert not any(key.startswith("transaction") for key in row)
            assert "tenderly_url" not in row


def test_only_anchor_quotes_request_encoding() -> None:
    # Encoding is billable work on the Fynd side and only the anchored levels
    # keep the calldata, so the ~200 sweep quotes and every probe stay bare.
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        order = payload["orders"][0]
        return httpx.Response(200, json=amm_sim.quote_response(order["token_in"], order["amount"]))

    with FyndClient(transport=httpx.MockTransport(handler)) as fynd:
        collect_snapshot(fynd, make_config(anchor_targets=(1.0,)))

    # `min_responses=0` is the anchor signature: wait for every solver pool.
    assert any(payload["options"]["min_responses"] == 0 for payload in payloads)
    for payload in payloads:
        is_anchor = payload["options"]["min_responses"] == 0
        assert ("encoding_options" in payload["options"]) is is_anchor


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
        assert row["solve_time_ms"] == amm_sim.SOLVE_TIME_MS
        assert row["mixed_block"] is False
        out_decimals = WETH.decimals if row["side"] == "buy" else USDC.decimals
        assert row["gas_cost_token_out"] == amm_sim.NET_GAS_DISCOUNT / 10**out_decimals
        assert int(row["amount_out"]) - int(row["amount_out_net_gas"]) == amm_sim.NET_GAS_DISCOUNT

    for row in anchor_rows:
        assert row["target_impact_pct"] == 1.0
        assert row["derived_from"] in ("nearest_real_quote", "anchored_bisection")
        assert "bound" in row and "target_reached" in row
