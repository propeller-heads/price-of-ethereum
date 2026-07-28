"""CLI tests: argument parsing, client construction guards, and the snapshot
command end-to-end over mocked Fynd + Tycho transports."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

import httpx
import pytest

import amm_sim
from price_of_ethereum.cli import (
    CHAIN_TYCHO_HOSTS,
    build_config,
    build_parser,
    main,
    make_tycho,
    run_collect,
    run_init_worker_pools,
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


def test_report_is_the_only_read_side_command() -> None:
    args = build_parser().parse_args(["report", "--out", "data"])
    assert args.output == "report.html"
    assert args.chain_id == 1
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve"])


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


# Chain ids per ethereum-lists/chains; hosts follow the per-chain Tycho pattern.
# These are the six chains Fynd routes on — Tycho also indexes starknet and
# zksync, which Fynd does not, so they are deliberately absent.
EXPECTED_CHAIN_HOSTS = [
    (1, "ethereum", "https://tycho-beta.propellerheads.xyz"),
    (56, "bsc", "https://tycho-bsc-beta.propellerheads.xyz"),
    (130, "unichain", "https://tycho-unichain-beta.propellerheads.xyz"),
    (137, "polygon", "https://tycho-polygon-beta.propellerheads.xyz"),
    (8453, "base", "https://tycho-base-beta.propellerheads.xyz"),
    (42161, "arbitrum", "https://tycho-arbitrum-beta.propellerheads.xyz"),
]


@pytest.mark.parametrize(("chain_id", "chain", "host"), EXPECTED_CHAIN_HOSTS)
def test_make_tycho_uses_per_chain_default_host(
    chain_id: int, chain: str, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYCHO_API_KEY", "test-key")
    args = build_parser().parse_args(["snapshot"])
    with make_tycho(args, chain_id=chain_id) as tycho:
        assert tycho.chain == chain
        assert str(tycho._http.base_url).startswith(host)


def test_every_chain_id_maps_to_a_distinct_host() -> None:
    # A copy-pasted hostname would silently resolve a token on the wrong chain.
    expected = {chain_id: (chain, host) for chain_id, chain, host in EXPECTED_CHAIN_HOSTS}
    assert expected == CHAIN_TYCHO_HOSTS
    assert len({host for _, host in CHAIN_TYCHO_HOSTS.values()}) == len(CHAIN_TYCHO_HOSTS)


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


def test_build_config_skips_tycho_when_both_tokens_are_overridden() -> None:
    args = build_parser().parse_args(
        [
            "snapshot",
            "--token-decimals",
            "18",
            "--token-symbol",
            "WETH",
            "--numeraire-decimals",
            "6",
            "--numeraire-symbol",
            "USDC",
        ]
    )
    config = build_config(args, chain_id=1, tycho=None)  # no Tycho client constructed at all
    assert config.token.symbol == "WETH"
    assert config.token.decimals == 18
    assert config.token.is_standard
    assert config.numeraire.symbol == "USDC"
    assert config.numeraire.decimals == 6


def test_build_config_resolves_only_the_unoverridden_token() -> None:
    args = build_parser().parse_args(
        ["snapshot", "--token-decimals", "18", "--token-symbol", "WETH"]
    )
    with tycho_client() as tycho:
        config = build_config(args, chain_id=1, tycho=tycho)
    assert config.token.symbol == "WETH"  # from the override, not Tycho
    assert config.numeraire.decimals == 6  # resolved via Tycho


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--token-decimals", "18"], "--token-decimals and --token-symbol"),
        (["--token-symbol", "WETH"], "--token-decimals and --token-symbol"),
        (["--numeraire-decimals", "6"], "--numeraire-decimals and --numeraire-symbol"),
        (["--numeraire-symbol", "USDC"], "--numeraire-decimals and --numeraire-symbol"),
    ],
)
def test_partial_token_override_fails_loudly(flags: list[str], expected: str) -> None:
    # Half an override is the dangerous case: pairing a stated decimals with a
    # resolved symbol, or the reverse, would measure against a mismatched pair
    # with nothing to show for it.
    args = build_parser().parse_args(["snapshot", *flags])
    with pytest.raises(SystemExit, match=expected):
        build_config(args, chain_id=1, tycho=None)


def test_tokens_need_tycho_when_no_override_describes_them() -> None:
    args = build_parser().parse_args(["snapshot"])
    with pytest.raises(SystemExit, match="needs Tycho"):
        build_config(args, chain_id=1, tycho=None)


def test_init_worker_pools_writes_the_packaged_file(tmp_path: Path) -> None:
    out_path = tmp_path / "worker_pools.toml"
    args = build_parser().parse_args(["init-worker-pools", "--out", str(out_path)])
    exit_code = run_init_worker_pools(args)
    assert exit_code == 0
    packaged = (
        importlib.resources.files("price_of_ethereum") / "data" / "worker_pools.toml"
    ).read_text(encoding="utf-8")
    assert out_path.read_text(encoding="utf-8") == packaged


def test_init_worker_pools_refuses_to_overwrite(tmp_path: Path) -> None:
    out_path = tmp_path / "worker_pools.toml"
    out_path.write_text("existing content", encoding="utf-8")
    args = build_parser().parse_args(["init-worker-pools", "--out", str(out_path)])
    with pytest.raises(SystemExit, match="already exists"):
        run_init_worker_pools(args)
    assert out_path.read_text(encoding="utf-8") == "existing content"


def test_init_worker_pools_overwrite_flag_replaces_the_file(tmp_path: Path) -> None:
    out_path = tmp_path / "worker_pools.toml"
    out_path.write_text("existing content", encoding="utf-8")
    args = build_parser().parse_args(["init-worker-pools", "--out", str(out_path), "--overwrite"])
    exit_code = run_init_worker_pools(args)
    assert exit_code == 0
    assert "existing content" not in out_path.read_text(encoding="utf-8")


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


def test_partial_override_fails_before_reaching_fynd() -> None:
    # Port 1 has nothing listening, so any attempt to connect raises a
    # connection error instead. Getting the flag message proves the check runs
    # first — Fynd cold start can take minutes, and waiting that out to be told
    # a flag is missing its pair is the behaviour this pins against.
    with pytest.raises(SystemExit, match="--token-decimals and --token-symbol"):
        main(["snapshot", "--fynd-url", "http://127.0.0.1:1", "--token-decimals", "18"])


@pytest.mark.parametrize("bad", ["0", "1", "1.5", "-0.01", "loose"])
def test_slippage_outside_the_open_unit_interval_is_rejected(bad: str) -> None:
    # A bad bound would otherwise look like thin liquidity: every anchor fails
    # to encode, minutes into a run, with nothing naming the cause.
    args = build_parser().parse_args(["snapshot", "--slippage", bad])
    with pytest.raises(SystemExit, match="--slippage"):
        build_config(args, chain_id=1, tycho=None)


def test_slippage_reaches_the_snapshot_config() -> None:
    args = build_parser().parse_args(
        [
            "snapshot",
            "--slippage",
            "0.02",
            "--token-decimals",
            "18",
            "--token-symbol",
            "WETH",
            "--numeraire-decimals",
            "6",
            "--numeraire-symbol",
            "USDC",
        ]
    )
    config = build_config(args, chain_id=1, tycho=None)
    assert config.slippage == "0.02"
