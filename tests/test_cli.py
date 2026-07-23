"""CLI tests: argument parsing, client construction guards, and the snapshot
command end-to-end over mocked Fynd + Tycho transports."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import amm_sim
from price_of_ethereum.cli import (
    build_config,
    build_parser,
    make_tycho,
    run_collect,
    run_snapshot,
)
from price_of_ethereum.fynd import FyndClient
from price_of_ethereum.storage import load_jsonl
from price_of_ethereum.tycho import TychoClient


def tycho_client() -> TychoClient:
    tokens = [
        {
            "chain": "ethereum",
            "address": amm_sim.WETH_ADDRESS,
            "symbol": "WETH",
            "decimals": 18,
            "tax": 0,
            "gas": [50000],
            "quality": 100,
        },
        {
            "chain": "ethereum",
            "address": amm_sim.USDC_ADDRESS,
            "symbol": "USDC",
            "decimals": 6,
            "tax": 0,
            "gas": [40000],
            "quality": 100,
        },
    ]
    page = {"tokens": tokens, "pagination": {"page": 0, "page_size": 100, "total": 2}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    return TychoClient("https://tycho.test", "key", transport=httpx.MockTransport(handler))


def amm_fynd_client() -> FyndClient:
    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        return httpx.Response(200, json=amm_sim.quote_response(order["token_in"], order["amount"]))

    return FyndClient(transport=httpx.MockTransport(handler))


def test_parser_defaults() -> None:
    args = build_parser().parse_args(["collect"])
    assert args.fynd_url == "http://127.0.0.1:3000"
    assert args.samples_per_side == 100
    assert args.search_min == 50.0
    assert args.search_max == 50_000_000.0
    assert args.blocks is None
    assert args.out == "data"


def test_make_tycho_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYCHO_API_KEY", raising=False)
    args = build_parser().parse_args(["snapshot"])
    with pytest.raises(SystemExit, match="Tycho API key"):
        make_tycho(args, chain_id=1)


def test_make_tycho_rejects_unknown_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYCHO_API_KEY", "test-key")
    args = build_parser().parse_args(["snapshot"])
    with pytest.raises(SystemExit, match="Unknown chain_id"):
        make_tycho(args, chain_id=999)


def test_make_tycho_uses_per_chain_default_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYCHO_API_KEY", "test-key")
    args = build_parser().parse_args(["snapshot"])
    with make_tycho(args, chain_id=8453) as tycho:
        assert tycho.chain == "base"
        assert str(tycho._http.base_url).startswith("https://tycho-base-beta")


def test_make_tycho_explicit_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYCHO_API_KEY", "test-key")
    args = build_parser().parse_args(["snapshot", "--tycho-url", "http://localhost:4242"])
    with make_tycho(args, chain_id=1) as tycho:
        assert tycho.chain == "ethereum"
        assert str(tycho._http.base_url).startswith("http://localhost:4242")


def test_build_config_resolves_tokens() -> None:
    args = build_parser().parse_args(["snapshot", "--samples-per-side", "4"])
    with tycho_client() as tycho:
        config = build_config(args, chain_id=1, tycho=tycho)
    assert config.token.symbol == "WETH"
    assert config.numeraire.decimals == 6
    assert config.chain_id == 1
    assert config.samples_per_side == 4


def test_run_snapshot_prints_summary_and_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = build_parser().parse_args(
        [
            "snapshot",
            "--write",
            "--out",
            str(tmp_path),
            "--samples-per-side",
            "8",
            "--search-max",
            "50000",
        ]
    )
    with amm_fynd_client() as fynd, tycho_client() as tycho:
        exit_code = run_snapshot(args, fynd, tycho, chain_id=1)

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["block_number"] == amm_sim.BLOCK_NUMBER
    assert summary["pair"] == "ETH/USDC"
    assert summary["robust_mid"] > 0
    assert summary["spot"] > 0
    assert summary["mid_source"] == "sweep_band"
    assert summary["mixed_block"] is False
    rows_frame = load_jsonl(tmp_path / "eth-usdc_1.rows.jsonl")
    assert len(rows_frame) > 0
    assert set(rows_frame["kind"]) == {"curve", "anchor"}
    assert {"execution_price", "price_impact_bps", "route_hash"} <= set(rows_frame.columns)
    blocks_frame = load_jsonl(tmp_path / "eth-usdc_1.blocks.jsonl")
    assert blocks_frame["block_number"].tolist() == [amm_sim.BLOCK_NUMBER]


def test_run_collect_records_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = build_parser().parse_args(
        [
            "collect",
            "--blocks",
            "1",
            "--out",
            str(tmp_path),
            "--samples-per-side",
            "8",
            "--search-max",
            "50000",
        ]
    )
    with amm_fynd_client() as fynd, tycho_client() as tycho:
        exit_code = run_collect(args, fynd, tycho, chain_id=1)

    assert exit_code == 0
    assert "recorded 1 blocks" in capsys.readouterr().err
    blocks_frame = load_jsonl(tmp_path / "eth-usdc_1.blocks.jsonl")
    assert blocks_frame["block_number"].tolist() == [amm_sim.BLOCK_NUMBER]
