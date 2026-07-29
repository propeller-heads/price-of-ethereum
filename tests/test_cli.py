"""CLI tests: argument parsing, client construction guards, and the snapshot
command end-to-end over mocked Fynd + Tycho transports."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import amm_sim
from price_of_ethereum.cli import (
    CHAIN_POLL_INTERVAL_S,
    CHAIN_TYCHO_HOSTS,
    USD_REFERENCE,
    build_config,
    build_parser,
    main,
    make_tycho,
    run_collect,
    run_init_worker_pools,
    run_snapshot,
)
from price_of_ethereum.collect import CollectionAbortedError
from price_of_ethereum.fynd import FyndClient
from price_of_ethereum.sizing import ReferenceRate
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
    # None, not the number: build_config needs to tell "unset" from "set to the
    # default" so it knows whether it may scale them into numeraire units.
    assert args.search_min is None
    assert args.search_max is None
    assert args.blocks is None
    assert args.out == "data"


@pytest.mark.parametrize("command", ["report", "serve"])
def test_commands_that_render_are_not_offered(command: str) -> None:
    # Charts are drawn in the notebook, from the recorded JSONL. The CLI
    # measures and writes; it renders nothing.
    with pytest.raises(SystemExit):
        build_parser().parse_args([command, "--out", "data"])


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


def test_mainnet_defaults_are_refused_on_another_chain() -> None:
    # Ethereum's WETH and USDC addresses are somebody else's contracts on BSC,
    # so quoting them there measures a pair nobody asked for.
    args = build_parser().parse_args(["snapshot"])
    with pytest.raises(SystemExit, match="Ethereum defaults"):
        build_config(args, chain_id=56, tycho=None)


def test_sizes_stay_in_numeraire_units_without_a_rate() -> None:
    # No Fynd to price the numeraire, so the dollar-shaped defaults are used as
    # numeraire units and the summary says the rate is unknown.
    args = build_parser().parse_args(
        [
            "snapshot",
            *("--token-decimals", "18", "--token-symbol", "WETH"),
            *("--numeraire-decimals", "6", "--numeraire-symbol", "USDC"),
        ]
    )
    config = build_config(args, chain_id=1, tycho=None)
    assert config.numeraire_usd is None
    assert (config.mid_band_min, config.mid_band_max) == (2_500.0, 10_000.0)
    assert config.search_min == 50.0
    assert config.probe_notional == 1000.0


def test_opting_out_of_the_usd_reference_keeps_numeraire_units() -> None:
    args = build_parser().parse_args(
        [
            "snapshot",
            *("--usd-reference", "none"),
            *("--token", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"),
            *("--numeraire", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
            *("--token-decimals", "18", "--token-symbol", "BTCB"),
            *("--numeraire-decimals", "18", "--numeraire-symbol", "WBNB"),
        ]
    )
    config = build_config(args, chain_id=56, tycho=None, fynd=None)
    assert config.numeraire_usd is None


def test_a_measured_rate_sizes_every_dollar_shaped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A numeraire worth $2,500 makes a $2,500 band one numeraire unit wide, so
    # every default has to divide by the rate rather than travel as dollars.
    monkeypatch.setattr(
        "price_of_ethereum.cli._numeraire_price_in_usd",
        lambda *_, **__: ReferenceRate(rate=2_500.0, spread=0.0004, block=25_632_157),
    )
    args = build_parser().parse_args(
        [
            "snapshot",
            *("--token", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"),
            *("--numeraire", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
            *("--token-decimals", "18", "--token-symbol", "BTCB"),
            *("--numeraire-decimals", "18", "--numeraire-symbol", "WBNB"),
        ]
    )
    config = build_config(args, chain_id=56, tycho=None, fynd=None)
    assert config.numeraire_usd == 2_500.0
    assert (config.mid_band_min, config.mid_band_max) == (1.0, 4.0)
    assert config.search_min == 50.0 / 2_500.0
    assert config.probe_notional == 1_000.0 / 2_500.0


def test_an_explicit_size_is_not_rescaled(monkeypatch: pytest.MonkeyPatch) -> None:
    # --search-min is already in numeraire units; scaling it would move a size
    # the caller measured for themselves.
    monkeypatch.setattr(
        "price_of_ethereum.cli._numeraire_price_in_usd",
        lambda *_, **__: ReferenceRate(rate=2_500.0, spread=0.0004, block=25_632_157),
    )
    args = build_parser().parse_args(
        [
            "snapshot",
            *("--search-min", "7.5"),
            *("--token", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"),
            *("--numeraire", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
            *("--token-decimals", "18", "--token-symbol", "BTCB"),
            *("--numeraire-decimals", "18", "--numeraire-symbol", "WBNB"),
        ]
    )
    assert build_config(args, chain_id=56, tycho=None, fynd=None).search_min == 7.5


def bsc_pair_args(*extra: str) -> Any:
    return build_parser().parse_args(
        [
            "snapshot",
            *extra,
            *("--token", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"),
            *("--numeraire", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
            *("--token-decimals", "18", "--token-symbol", "BTCB"),
            *("--numeraire-decimals", "18", "--numeraire-symbol", "WBNB"),
        ]
    )


def test_describing_both_tokens_still_runs_without_tycho() -> None:
    # Describing both tokens is how a run avoids needing a Tycho key. Reading the
    # reference's decimals must not hand that requirement back: the rate is worth
    # having, not worth refusing to measure anything over.
    config = build_config(bsc_pair_args(), chain_id=56, tycho=None, fynd=amm_fynd_client())
    assert config.numeraire_usd is None
    assert (config.mid_band_min, config.mid_band_max) == (2_500.0, 10_000.0)


def test_a_named_reference_without_decimals_is_refused() -> None:
    # Naming the reference is asking for the rate, so failing to size it is an
    # error rather than something to degrade past.
    args = bsc_pair_args("--usd-reference", "0x55d398326f99059fF775485246999027B3197955")
    with pytest.raises(SystemExit, match="needs its decimals"):
        build_config(args, chain_id=56, tycho=None, fynd=amm_fynd_client())


def priced_bsc_config(monkeypatch: pytest.MonkeyPatch, *, spread: float) -> Any:
    monkeypatch.setattr(
        "price_of_ethereum.cli.reference_rate",
        lambda *_, **__: ReferenceRate(rate=2_500.0, spread=spread, block=25_632_157),
    )
    args = bsc_pair_args("--usd-reference-decimals", "18")
    return build_config(args, chain_id=56, tycho=None, fynd=amm_fynd_client())


BSC_USD = "0x55d398326f99059fF775485246999027B3197955"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"


def bnb_fynd_client(bnb_in_dollars: float = 600.0) -> FyndClient:
    """A Fynd where one WBNB is worth `bnb_in_dollars` BSC-USD, both 18 decimals."""

    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["orders"][0]
        amount_in = int(order["amount"])
        buying_bnb = order["token_in"].lower() == BSC_USD.lower()
        rate = 1.0 / bnb_in_dollars if buying_bnb else bnb_in_dollars
        amount_out = int(amount_in * rate)
        return httpx.Response(
            200,
            json={
                "orders": [
                    {
                        "order_id": "x",
                        "status": "success",
                        "amount_in": str(amount_in),
                        "amount_out": str(amount_out),
                        "amount_out_net_gas": str(amount_out),
                        "gas_estimate": "150000",
                        "block": {"number": 44_000_000, "hash": "0xabc", "timestamp": 1},
                    }
                ],
                "total_gas_estimate": "150000",
                "solve_time_ms": 3,
            },
        )

    return FyndClient(transport=httpx.MockTransport(handler))


def test_build_config_prices_the_numeraire_against_the_reference_not_the_reverse() -> None:
    # Reaches the real reference_rate rather than a stub, so a swapped
    # numeraire/reference pair at the call site is caught: inverted, the rate
    # would be 1/600 and the band would come out at 1.5M WBNB rather than 4.17.
    args = bsc_pair_args("--usd-reference-decimals", "18")
    config = build_config(args, chain_id=56, tycho=None, fynd=bnb_fynd_client())
    assert config.numeraire_usd == pytest.approx(600.0, rel=1e-6)
    assert config.mid_band_min == pytest.approx(2_500.0 / 600.0, rel=1e-6)
    assert config.mid_band_max == pytest.approx(10_000.0 / 600.0, rel=1e-6)
    assert config.numeraire_usd_block == 44_000_000
    assert config.numeraire_usd_spread_bps == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize(
    ("spread", "priced"),
    [(0.02, True), (0.0201, False), (-0.005, True), (-0.25, False)],
)
def test_the_spread_gate_is_symmetric_and_inclusive_at_its_bound(
    monkeypatch: pytest.MonkeyPatch, spread: float, priced: bool
) -> None:
    # A pair whose round trip lands exactly on the tolerance is still usable, and
    # a bid above the ask is a quote to distrust by the same margin either way.
    config = priced_bsc_config(monkeypatch, spread=spread)
    assert (config.numeraire_usd is not None) is priced


def test_a_priced_reference_sizes_the_band_and_records_its_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rate is measured once for the run and written to every block row, so
    # the row carries the block it came from or its age cannot be judged.
    config = priced_bsc_config(monkeypatch, spread=0.005)
    assert config.numeraire_usd == 2_500.0
    assert (config.mid_band_min, config.mid_band_max) == (1.0, 4.0)
    assert config.numeraire_usd_block == 25_632_157


def test_a_reference_pair_too_thin_to_price_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wide round trip means the rate is mostly the probe's own impact. Scaling
    # by it would produce a band that looks reasonable and measures the wrong
    # depths, so the run keeps raw numeraire units instead.
    config = priced_bsc_config(monkeypatch, spread=0.25)
    assert config.numeraire_usd is None
    assert config.numeraire_usd_block is None
    assert (config.mid_band_min, config.mid_band_max) == (2_500.0, 10_000.0)


def test_every_supported_chain_has_a_usd_reference() -> None:
    # A chain with no reference sizes its band in raw numeraire units, which on
    # a WBNB or WETH pair is millions of dollars wide.
    assert set(USD_REFERENCE) == set(CHAIN_TYCHO_HOSTS)


def test_every_supported_chain_has_a_poll_interval() -> None:
    # A chain we ship a Tycho host for is a chain someone will collect on, and
    # the fallback is tuned for nothing in particular.
    assert set(CHAIN_POLL_INTERVAL_S) == set(CHAIN_TYCHO_HOSTS)


def test_poll_intervals_sit_between_the_probe_cost_and_the_block_time() -> None:
    # Measured 2026-07-28: block interval and the cost of one probe, which is a
    # route solve because Fynd reports a block number only inside a quote.
    measured = {  # chain_id: (block_s, probe_s)
        1: (12.0, 0.075),
        56: (0.446, 0.114),
        130: (1.0, 0.013),
        137: (1.571, 0.043),
        8453: (2.0, 0.063),
        42161: (0.253, 0.100),
    }
    for chain_id, (block_s, probe_s) in measured.items():
        interval = CHAIN_POLL_INTERVAL_S[chain_id]
        assert interval >= probe_s, f"chain {chain_id} polls faster than one probe returns"
        # A cycle is a probe then a sleep, so the probe is what has to fit too.
        assert interval + probe_s < block_s, f"chain {chain_id} cannot poll twice in a block"


def test_poll_interval_flag_overrides_the_table() -> None:
    args = build_parser().parse_args(["collect", "--poll-interval-s", "0.5"])
    assert args.poll_interval_s == 0.5
    assert build_parser().parse_args(["collect"]).poll_interval_s is None


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(["--poll-interval-s", "0"], 0.0), ([], 0.25)],
)
def test_run_collect_honours_an_explicit_zero_poll_interval(
    monkeypatch: pytest.MonkeyPatch, flag: list[str], expected: float
) -> None:
    # 0.0 is falsy but meaningful: poll as fast as Fynd answers.
    passed: dict[str, float] = {}

    def capture(fynd: object, config: object, **kwargs: Any) -> object:
        passed["poll_interval_s"] = kwargs["poll_interval_s"]
        raise CollectionAbortedError("stop after capturing the interval")

    monkeypatch.setattr("price_of_ethereum.cli.collect_blocks", capture)
    args = build_parser().parse_args(
        [
            "collect",
            *flag,
            *("--token-decimals", "18", "--token-symbol", "WETH"),
            *("--numeraire-decimals", "6", "--numeraire-symbol", "USDC"),
        ]
    )
    with pytest.raises(CollectionAbortedError):
        run_collect(args, cast(FyndClient, None), None, chain_id=1)
    assert passed["poll_interval_s"] == expected


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "fast"])
def test_unusable_poll_intervals_are_refused_before_fynd(value: str) -> None:
    # time.sleep raises on these mid-run; argparse should reject them at parse.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["collect", "--poll-interval-s", value])


@pytest.mark.parametrize("value", ["-1", "256", "six"])
def test_unusable_reference_decimals_are_refused_before_fynd(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["snapshot", "--usd-reference-decimals", value])
