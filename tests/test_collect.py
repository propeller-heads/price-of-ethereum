"""Collector-loop tests over AMM mocks that drive the simulated block explicitly.

The clock probe and the snapshot's own spot probe are byte-identical requests, so
a mock cannot tell them apart and counting "probes" is ambiguous. The mocks here
key on unambiguous signals instead:

  `sweep_paced_client`  advances the block once per completed sweep, spotted by
                        the grid's top rung — exactly one per snapshot. Probes
                        never move it, so every cycle measures a fresh block.
  `probe_paced_client`  advances after a fixed number of probes, modelling wall
                        time passing while the collector waits for a new block.

Both are deterministic; neither depends on timing.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import amm_sim
from price_of_ethereum import FyndClient, SnapshotConfig, TokenMeta
from price_of_ethereum.collect import CollectionAbortedError, collect_blocks, output_paths
from price_of_ethereum.sizing import atomic
from price_of_ethereum.snapshot import collect_snapshot
from price_of_ethereum.storage import load_jsonl

WETH = TokenMeta(address=amm_sim.WETH_ADDRESS, symbol="WETH", decimals=18, quality=100, tax=0)
USDC = TokenMeta(address=amm_sim.USDC_ADDRESS, symbol="USDC", decimals=6, quality=100, tax=0)

SPOT_PROBE_AMOUNT = atomic(1000.0, USDC.decimals)


def make_config() -> SnapshotConfig:
    # Grid values (50, 500, 5000, 50000) never collide with the $1000 probe.
    return SnapshotConfig(
        token=WETH,
        numeraire=USDC,
        pair="ETH/USDC",
        chain_id=1,
        search_min=50.0,
        search_max=50_000.0,
        samples_per_side=4,
        impact_levels=(1.0,),
        anchor_targets=(),
        max_workers=2,
    )


# Top buy rung of make_config()'s grid: seen exactly once per completed sweep.
TOP_RUNG_AMOUNT = atomic(50_000.0, USDC.decimals)


def is_probe_request(order: dict) -> bool:
    return (
        order["token_in"].lower() == amm_sim.USDC_ADDRESS.lower()
        and int(order["amount"]) == SPOT_PROBE_AMOUNT
    )


def sweep_paced_client() -> FyndClient:
    """Block advances once per completed sweep, so every cycle sees a new one."""
    state = {"block": amm_sim.BLOCK_NUMBER}

    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        body["orders"][0]["block"]["number"] = state["block"]
        if int(order["amount"]) == TOP_RUNG_AMOUNT:
            state["block"] += 1
        return httpx.Response(200, json=body)

    return FyndClient(transport=httpx.MockTransport(handler))


def probe_paced_client(*, advance_after_probes: int) -> FyndClient:
    """Block advances every `advance_after_probes` probes, as wall time would."""
    state = {"probes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        if is_probe_request(order):
            state["probes"] += 1
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        body["orders"][0]["block"]["number"] = (
            amm_sim.BLOCK_NUMBER + (state["probes"] - 1) // advance_after_probes
        )
        return httpx.Response(200, json=body)

    return FyndClient(transport=httpx.MockTransport(handler))


def test_collect_records_distinct_blocks(tmp_path: Path) -> None:
    config = make_config()
    with sweep_paced_client() as fynd:
        result = collect_blocks(fynd, config, out_dir=tmp_path, blocks=3, idle_wait_s=0.0)

    assert result.blocks_recorded == 3
    assert result.duplicate_snapshots == 0
    assert result.failed_cycles == 0
    assert result.interrupted is False
    blocks_frame = load_jsonl(result.blocks_path)
    recorded = blocks_frame["block_number"].tolist()
    assert recorded == sorted(set(recorded))  # distinct and strictly increasing
    assert len(recorded) == 3
    assert recorded[0] == amm_sim.BLOCK_NUMBER
    # A fresh block every cycle, so the cheap clock never has to wait.
    assert result.idle_probes == 0
    rows_frame = load_jsonl(result.rows_path)
    assert result.rows_written == len(rows_frame) > 0
    assert set(rows_frame["kind"]) == {"curve", "anchor"}
    assert rows_frame["pair"].unique().tolist() == ["ETH/USDC"]


def test_collect_skips_duplicate_blocks(tmp_path: Path) -> None:
    config = make_config()
    with probe_paced_client(advance_after_probes=3) as fynd:
        result = collect_blocks(fynd, config, out_dir=tmp_path, blocks=2, idle_wait_s=0.0)

    assert result.blocks_recorded == 2
    # The cheap clock absorbs the wait, so no full sweep is thrown away.
    assert result.idle_probes > 0
    assert result.duplicate_snapshots == 0
    blocks_frame = load_jsonl(result.blocks_path)
    assert blocks_frame["block_number"].is_unique


def test_idle_cycle_costs_exactly_one_quote(tmp_path: Path) -> None:
    """The whole point of the probe: waiting for a block must not sweep."""
    requests: list[int] = []
    state = {"block": amm_sim.BLOCK_NUMBER, "probes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        requests.append(int(order["amount"]))
        is_probe = (
            order["token_in"].lower() == amm_sim.USDC_ADDRESS.lower()
            and int(order["amount"]) == SPOT_PROBE_AMOUNT
        )
        # Hold the block still for three probes, then let it advance once.
        if is_probe:
            state["probes"] += 1
            if state["probes"] > 4:
                state["block"] = amm_sim.BLOCK_NUMBER + 1
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        body["orders"][0]["block"]["number"] = state["block"]
        return httpx.Response(200, json=body)

    config = make_config()
    with sweep_paced_client() as seed:
        first = collect_blocks(
            fynd=seed, config=config, out_dir=tmp_path, blocks=1, idle_wait_s=0.0
        )
    assert first.blocks_recorded == 1

    with FyndClient(transport=httpx.MockTransport(handler)) as fynd:
        requests.clear()
        result = collect_blocks(fynd, config, out_dir=tmp_path, blocks=1, idle_wait_s=0.0)

    assert result.blocks_recorded == 1
    assert result.idle_probes >= 3
    # Every idle cycle is one probe-sized quote; only the final sweep is bigger.
    probe_only = [amount for amount in requests[: result.idle_probes]]
    assert probe_only == [SPOT_PROBE_AMOUNT] * result.idle_probes
    # A full sweep of this config is far more than the handful of idle probes.
    assert len(requests) > result.idle_probes + 8


def test_collect_raises_after_consecutive_failures(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        body["orders"][0]["status"] = "not_ready"
        return httpx.Response(200, json=body)

    config = make_config()
    with (
        FyndClient(transport=httpx.MockTransport(handler)) as fynd,
        pytest.raises(CollectionAbortedError, match="3 consecutive"),
    ):
        collect_blocks(
            fynd, config, out_dir=tmp_path, blocks=1, idle_wait_s=0.0, max_consecutive_failures=3
        )
    rows_path, blocks_path = output_paths(tmp_path, config)
    assert not rows_path.exists() and not blocks_path.exists()


def test_collect_recovers_after_transient_failures(tmp_path: Path) -> None:
    # Fails probes 1-2, succeeds, fails probes 4-5, succeeds: with the cap at 3
    # this only completes if the consecutive-failure counter resets on success.
    failing_probes = {1, 2, 4, 5}
    state = {"probes": 0, "block": amm_sim.BLOCK_NUMBER}

    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        body = amm_sim.quote_response(order["token_in"], order["amount"])
        is_probe = (
            order["token_in"].lower() == amm_sim.USDC_ADDRESS.lower()
            and int(order["amount"]) == SPOT_PROBE_AMOUNT
        )
        if is_probe:
            state["probes"] += 1
            if state["probes"] in failing_probes:
                body["orders"][0]["status"] = "not_ready"
                return httpx.Response(200, json=body)
            state["block"] = amm_sim.BLOCK_NUMBER + state["probes"]
        body["orders"][0]["block"]["number"] = state["block"]
        return httpx.Response(200, json=body)

    with FyndClient(transport=httpx.MockTransport(handler)) as fynd:
        result = collect_blocks(
            fynd,
            make_config(),
            out_dir=tmp_path,
            blocks=2,
            idle_wait_s=0.0,
            max_consecutive_failures=3,
        )
    assert result.blocks_recorded == 2
    # Recovery is the point: the run only finishes if the consecutive-failure
    # counter resets after each success, whatever the exact failure tally.
    assert result.failed_cycles >= 2
    assert result.interrupted is False


def test_collect_resumes_past_block_already_on_disk(tmp_path: Path) -> None:
    config = make_config()
    with sweep_paced_client() as fynd:
        first = collect_blocks(fynd, config, out_dir=tmp_path, blocks=1, idle_wait_s=0.0)
    assert load_jsonl(first.blocks_path)["block_number"].tolist() == [amm_sim.BLOCK_NUMBER]

    # A fresh client re-serves BLOCK_NUMBER first; the restarted collector must
    # skip it (seeded from disk) and record the next block instead. Probe pacing
    # is required here: the block has to advance while the collector waits, which
    # is exactly what it refuses to force by sweeping.
    with probe_paced_client(advance_after_probes=1) as fynd:
        second = collect_blocks(fynd, config, out_dir=tmp_path, blocks=1, idle_wait_s=0.0)
    assert second.idle_probes == 1
    assert second.duplicate_snapshots == 0
    recorded = load_jsonl(second.blocks_path)["block_number"].tolist()
    assert recorded == sorted(set(recorded))
    assert len(recorded) == 2
    assert recorded[0] == amm_sim.BLOCK_NUMBER


def test_collect_returns_partial_result_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config()
    with sweep_paced_client() as fynd:
        recorded_snapshot = collect_snapshot(fynd, config)

        calls = {"count": 0}

        def snapshot_then_interrupt(*args: object, **kwargs: object):
            calls["count"] += 1
            if calls["count"] == 1:
                return recorded_snapshot
            raise KeyboardInterrupt

        monkeypatch.setattr("price_of_ethereum.collect.collect_snapshot", snapshot_then_interrupt)
        result = collect_blocks(fynd, config, out_dir=tmp_path, blocks=5, idle_wait_s=0.0)

    assert result.interrupted is True
    assert result.blocks_recorded == 1
    assert load_jsonl(result.blocks_path)["block_number"].tolist() == [amm_sim.BLOCK_NUMBER]


def test_output_paths_slug(tmp_path: Path) -> None:
    rows_path, blocks_path = output_paths(tmp_path, make_config())
    assert rows_path.name == "eth-usdc_1.rows.jsonl"
    assert blocks_path.name == "eth-usdc_1.blocks.jsonl"
