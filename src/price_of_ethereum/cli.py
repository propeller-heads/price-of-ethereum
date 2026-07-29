"""`poe` — thin CLI over the library.

Two write-side commands measure (`snapshot`, `collect`) and need a local Fynd.
Token metadata (decimals, symbol) normally comes from Tycho — a key from
`--tycho-api-key` or the `TYCHO_API_KEY` environment variable (free key from
https://t.me/fynd_portal_bot) — but `--token-decimals`/`--token-symbol` and
`--numeraire-decimals`/`--numeraire-symbol` let a self-hosted Tycho or no-Tycho
user describe a token directly and skip that lookup for it.

All measurement logic lives in the library; the CLI only builds a
`SnapshotConfig` and drives it.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.resources
import json
import logging
import math
import os
import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from price_of_ethereum import __version__
from price_of_ethereum.collect import (
    CollectionAbortedError,
    collect_blocks,
    output_paths,
)
from price_of_ethereum.fynd.client import (
    DEFAULT_SLIPPAGE,
    DUMMY_SENDER,
    FyndClient,
    FyndError,
)
from price_of_ethereum.pricing import ROBUST_MID_MAX_DEPTH, ROBUST_MID_MIN_DEPTH
from price_of_ethereum.sizing import ReferenceRate, SpotProbeError, reference_rate
from price_of_ethereum.snapshot import SnapshotConfig, collect_snapshot
from price_of_ethereum.storage import append_jsonl
from price_of_ethereum.tokens import TokenMeta, resolve_tokens
from price_of_ethereum.tycho.client import TychoClient, TychoError
from price_of_ethereum.tycho.models import Chain

WETH_MAINNET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC_MAINNET = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

DEFAULT_FYND_URL = "http://127.0.0.1:3000"

# Fynd's /v1/info reports a numeric chain id; Tycho wants a chain name, and the
# hosted Tycho deployment runs one host per chain. Every chain Fynd routes on is
# here; Tycho also indexes starknet and zksync, which Fynd does not route.
CHAIN_TYCHO_HOSTS: dict[int, tuple[Chain, str]] = {
    1: ("ethereum", "https://tycho-beta.propellerheads.xyz"),
    56: ("bsc", "https://tycho-bsc-beta.propellerheads.xyz"),
    130: ("unichain", "https://tycho-unichain-beta.propellerheads.xyz"),
    137: ("polygon", "https://tycho-polygon-beta.propellerheads.xyz"),
    8453: ("base", "https://tycho-base-beta.propellerheads.xyz"),
    42161: ("arbitrum", "https://tycho-arbitrum-beta.propellerheads.xyz"),
}

# A dollar-denominated reference per chain, used to express the dollar-shaped
# defaults (the robust-mid band, the spot probe, the grid bounds) in whatever
# numeraire a run actually uses. Native Circle USDC where it exists; BNB Smart
# Chain has none, so the deepest bridged dollar stands in. Decimals are NOT
# recorded here on purpose — BSC-USD carries 18 where USDC carries 6, and a
# wrong constant would misprice the band silently. They come from Tycho.
USD_REFERENCE: dict[int, str] = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    56: "0x55d398326f99059fF775485246999027B3197955",  # BSC-USD (Binance-Peg)
    130: "0x078D782b760474a361dDA0AF3839290b0EF57AD6",  # USDC
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # USDC
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC
}

# Probe size for pricing the numeraire against that reference, in dollars. Small
# enough to read as a marginal rate rather than a trade with its own impact.
USD_REFERENCE_PROBE = 1_000.0

# Round-trip cost above which the reference pair is too thin to price against.
# A dollar probe on a chain's main stablecoin pair crosses one or two fee tiers,
# so a healthy round trip costs well under 1%; past this the measured rate is
# mostly the probe's own impact, and a band scaled by it would be wrong in a way
# nothing downstream can detect.
MAX_REFERENCE_SPREAD = 0.02

# ERC-20 reports decimals as a uint8, so anything above this is a typo.
MAX_TOKEN_DECIMALS = 255

# How often to ask Fynd whether the block has moved, per chain, in seconds.
#
# Two things bound this and they pull in opposite directions. Fynd exposes no
# block number outside a quote, so each poll is a real route solve: on the
# chains below a single one costs roughly 13ms (unichain) to 114ms (bsc),
# measured 2026-07-28 as sweep duration over quote count. Polling faster than
# that just queues requests. Meanwhile a poll interval near the block time means
# discovering a block late and losing that much of the window the sweep needs.
#
# So each value sits well above its chain's probe cost and well below its block
# interval, both measured the same day. A cycle is one probe plus one sleep, not
# the sleep alone, so the probe cost is what decides how much a shorter interval
# can still buy:
#
#   chain      block     probe    interval   cycle    polls/block   note
#   ethereum   ~12s      ~75ms    0.25s      0.325s   ~37           lag is 3% of a block
#   base       2s        ~63ms    0.20s      0.263s   ~7.6
#   polygon    1.571s    ~43ms    0.15s      0.193s   ~8.1          block alternates 1s/2s
#   unichain   1s        ~13ms    0.10s      0.113s   ~8.8          cheap probes, thin graph
#   bsc        0.446s    ~114ms   0.15s      0.264s   ~1.7          solver-bound, not sleep-bound
#   arbitrum   0.253s    ~100ms   0.10s      0.200s   ~1.3          solver-bound, at the floor
#
# On bsc and arbitrum the probe itself is a large fraction of a block, so no
# value here detects every block — a cycle costs most of one, and the sweep
# overruns the block anyway. They are set at the probe floor rather than
# pretending otherwise.
#
# An unlisted chain gets DEFAULT_POLL_INTERVAL_S, deliberately conservative: too
# slow loses blocks on a fast chain, too fast burns solver time on a slow one,
# and losing blocks is the more visible failure.
CHAIN_POLL_INTERVAL_S: dict[int, float] = {
    1: 0.25,
    56: 0.15,
    130: 0.10,
    137: 0.15,
    8453: 0.20,
    42161: 0.10,
}
DEFAULT_POLL_INTERVAL_S = 0.25

# The sweep parameters have one source of truth: SnapshotConfig's defaults.
SNAPSHOT_DEFAULTS: dict[str, Any] = {
    field.name: field.default for field in dataclass_fields(SnapshotConfig)
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poe",
        description="Measure block-level onchain price and depth from your own local Fynd.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"poe {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("snapshot", "Collect one block snapshot and print its summary."),
        ("collect", "Record snapshots block by block into JSONL files."),
    ):
        sub = subparsers.add_parser(
            name, help=help_text, formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )
        sub.add_argument(
            "--fynd-url", default=DEFAULT_FYND_URL, help="Fynd instance to measure against."
        )
        sub.add_argument(
            "--sender",
            default=DUMMY_SENDER,
            help=(
                "Address the quotes are encoded for. Defaults to a placeholder that holds "
                "nothing, so anchor simulation links revert on the transfer; pass a funded "
                "address to get links that simulate the trade."
            ),
        )
        sub.add_argument(
            "--tycho-url",
            default=None,
            help=(
                "Tycho used for token metadata only. Defaults to the hosted host for the "
                "chain Fynd reports; point it at a self-hosted Tycho to override. To skip "
                "Tycho entirely, describe both tokens with --token-decimals/--token-symbol "
                "and --numeraire-decimals/--numeraire-symbol, which needs no key."
            ),
        )
        sub.add_argument(
            "--tycho-api-key",
            default=None,
            help="Defaults to the TYCHO_API_KEY environment variable.",
        )
        sub.add_argument("--token", default=WETH_MAINNET, help="Traded token address.")
        sub.add_argument(
            "--token-decimals",
            type=int,
            default=None,
            help="Decimals for --token; with --token-symbol, skips Tycho for this token.",
        )
        sub.add_argument(
            "--token-symbol",
            default=None,
            help="Symbol for --token; with --token-decimals, skips Tycho for this token.",
        )
        sub.add_argument("--numeraire", default=USDC_MAINNET, help="Numeraire token address.")
        sub.add_argument(
            "--numeraire-decimals",
            type=int,
            default=None,
            help="Decimals for --numeraire; with --numeraire-symbol, skips Tycho for it.",
        )
        sub.add_argument(
            "--numeraire-symbol",
            default=None,
            help="Symbol for --numeraire; with --numeraire-decimals, skips Tycho for it.",
        )
        sub.add_argument("--pair", default="ETH/USDC", help="Label stored on every row.")
        sub.add_argument(
            "--samples-per-side",
            type=int,
            default=SNAPSHOT_DEFAULTS["samples_per_side"],
            help=(
                "Trade sizes quoted per side. Part of how robust_mid is defined, not a "
                "resolution knob: lowering it changes the measurement."
            ),
        )
        sub.add_argument(
            "--search-min",
            type=float,
            default=None,
            help="Smallest trade size to quote, in whole numeraire units (USDC by default).",
        )
        sub.add_argument(
            "--search-max",
            type=float,
            default=None,
            help="Largest trade size to quote, in whole numeraire units (USDC by default).",
        )
        sub.add_argument(
            "--max-workers",
            type=int,
            default=SNAPSHOT_DEFAULTS["max_workers"],
            help="Concurrent quotes in flight against Fynd.",
        )
        sub.add_argument(
            "--wait-ready-s",
            type=float,
            default=300.0,
            help="Seconds to wait for Fynd to finish cold-start hydration.",
        )
        sub.add_argument(
            "--poll-interval-s",
            type=_validated_poll_interval,
            default=None,
            help=(
                "Seconds between asking Fynd whether the block moved. Defaults to a "
                "per-chain value; each poll is a route solve, so the useful floor is "
                "your pair's "
                "solve time, which the rows record as solve_time_ms."
            ),
        )
        sub.add_argument(
            "--usd-reference",
            default=None,
            help=(
                "Token standing in for a dollar, used to size the defaults in this pair's "
                "numeraire. Defaults to the chain's stablecoin; pass 'none' to keep every "
                "size in raw numeraire units."
            ),
        )
        sub.add_argument(
            "--usd-reference-decimals",
            type=_validated_decimals,
            default=None,
            help="Decimals for --usd-reference, so the rate needs no Tycho lookup.",
        )
        sub.add_argument(
            "--slippage",
            default=DEFAULT_SLIPPAGE,
            help=(
                "Slippage bound the anchored levels' calldata is encoded for, as a decimal "
                "fraction. The default is tight enough that Fynd declines to encode the "
                "largest sizes; those levels are still measured, but without a transaction."
            ),
        )
        sub.add_argument("--out", default="data", help="Output directory for JSONL files.")

    snapshot_parser = subparsers.choices["snapshot"]
    snapshot_parser.add_argument(
        "--write",
        action="store_true",
        help="Also append rows + block summary under --out (default: print only).",
    )
    collect_parser = subparsers.choices["collect"]
    collect_parser.add_argument(
        "--blocks",
        type=int,
        default=None,
        help="Number of distinct blocks to record (default: run until Ctrl-C).",
    )

    init_worker_pools_parser = subparsers.add_parser(
        "init-worker-pools",
        help="Write the packaged worker_pools.toml, tuned to keep snapshots inside one block.",
    )
    init_worker_pools_parser.add_argument(
        "--out", default="worker_pools.toml", help="Path to write the config to."
    )
    init_worker_pools_parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing file at --out."
    )
    return parser


def _validated_slippage(value: str) -> str:
    """Fynd wants a decimal string; reject anything outside (0, 1) up front.

    A bad bound would otherwise surface as an encoding failure per anchor,
    minutes into a run, looking like thin liquidity rather than a typo.
    """
    try:
        fraction = float(value)
    except ValueError:
        raise SystemExit(f"--slippage must be a decimal fraction, got {value!r}.") from None
    if not 0 < fraction < 1:
        raise SystemExit(f"--slippage must be between 0 and 1 exclusive, got {value!r}.")
    return value


def _validated_poll_interval(value: str) -> float:
    """A poll interval reaches `time.sleep`, which rejects negatives and NaN with
    a traceback minutes into a run. Reject them here, before Fynd is contacted.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise SystemExit(f"--poll-interval-s must be a number of seconds, got {value!r}.") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise SystemExit(f"--poll-interval-s must be finite and non-negative, got {value!r}.")
    return seconds


def _validated_decimals(value: str) -> int:
    """Decimals scale a probe by `10**decimals`, so a negative turns that into a
    float and a huge one allocates for a long time. Both are typos, not inputs.
    """
    try:
        places = int(value)
    except ValueError:
        raise SystemExit(f"--usd-reference-decimals must be an integer, got {value!r}.") from None
    if not 0 <= places <= MAX_TOKEN_DECIMALS:
        raise SystemExit(
            f"--usd-reference-decimals must be between 0 and {MAX_TOKEN_DECIMALS}, got {value!r}."
        )
    return places


def _token_override(args: argparse.Namespace, prefix: str) -> TokenMeta | None:
    """Build a `TokenMeta` from `--{prefix}-decimals`/`--{prefix}-symbol`, or
    `None` if neither was given.

    The two flags are all-or-nothing: giving only one leaves the token
    half-described, and silently falling back to Tycho for the missing field
    risks pairing a user-asserted decimals with a resolver-guessed symbol (or
    vice versa) without either side noticing, so this fails loudly instead.
    An override is assumed to be a standard token (quality 100, no tax) —
    that's what `--{prefix}-decimals`/`--{prefix}-symbol` are for; a
    non-standard token still needs Tycho's quality/tax classification.
    """
    address = args.token if prefix == "token" else args.numeraire
    decimals = getattr(args, f"{prefix}_decimals")
    symbol = getattr(args, f"{prefix}_symbol")
    if decimals is None and symbol is None:
        return None
    if decimals is None or symbol is None:
        raise SystemExit(
            f"--{prefix}-decimals and --{prefix}-symbol must be given together "
            f"to skip Tycho for --{prefix}."
        )
    return TokenMeta(address=address, symbol=symbol, decimals=decimals, quality=100, tax=0)


def _tycho_required(args: argparse.Namespace) -> bool:
    """False only when both token and numeraire are fully described by flags,
    letting a self-hosted-Tycho or no-Tycho user measure without a key."""
    return _token_override(args, "token") is None or _token_override(args, "numeraire") is None


def _numeraire_price_in_usd(
    args: argparse.Namespace,
    chain_id: int,
    numeraire: TokenMeta,
    fynd: FyndClient | None,
    tycho: TychoClient | None,
) -> ReferenceRate | None:
    """What one numeraire unit is worth in dollars, measured through Fynd.

    None means sizes stay in raw numeraire units: either there is no Fynd to ask,
    no reference for this chain, the caller opted out, the quote failed, or the
    reference pair was too thin to price against. The caller records which,
    because a band of 2,500 means something very different in USDC than in BNB.
    """
    choice = args.usd_reference
    if choice is not None and choice.lower() == "none":
        return None
    reference = choice or USD_REFERENCE.get(chain_id)
    if reference is None or fynd is None:
        return None
    if reference.lower() == numeraire.address.lower():
        return ReferenceRate(rate=1.0, spread=0.0, block=None)
    decimals = args.usd_reference_decimals
    if decimals is None:
        if tycho is None:
            # Describing both tokens is how a run avoids needing Tycho at all, so
            # the reference's decimals cannot demand it back. A reference the
            # caller named themselves is different: they asked for this rate.
            if choice is not None:
                raise SystemExit(
                    f"--usd-reference {reference} needs its decimals: pass "
                    "--usd-reference-decimals, or allow Tycho to resolve them."
                )
            logging.getLogger(__name__).warning(
                "no Tycho to read %s's decimals, so %s is unpriced; sizes stay in "
                "numeraire units. Pass --usd-reference-decimals to size them.",
                reference,
                numeraire.symbol,
            )
            return None
        decimals = resolve_tokens(tycho, [reference])[reference.lower()].decimals
    try:
        measured = reference_rate(
            fynd,
            numeraire=numeraire.address,
            numeraire_decimals=numeraire.decimals,
            reference=reference,
            reference_decimals=decimals,
            probe_notional=USD_REFERENCE_PROBE,
        )
    except SpotProbeError as error:
        logging.getLogger(__name__).warning(
            "no %s/%s route to price the numeraire (%s); sizes stay in numeraire units",
            numeraire.symbol,
            reference,
            error,
        )
        return None
    if abs(measured.spread) > MAX_REFERENCE_SPREAD:
        logging.getLogger(__name__).warning(
            "%s/%s costs %.2f%% to round-trip at %.0f, too thin to price against; "
            "sizes stay in numeraire units",
            numeraire.symbol,
            reference,
            measured.spread * 100.0,
            USD_REFERENCE_PROBE,
        )
        return None
    return measured


def build_config(
    args: argparse.Namespace,
    chain_id: int,
    tycho: TychoClient | None,
    fynd: FyndClient | None = None,
) -> SnapshotConfig:
    # Argument-only checks first: they cost nothing and a run that is going to
    # fail should fail before it waits on Fynd or reaches for Tycho.
    slippage = _validated_slippage(args.slippage)
    # WETH and USDC default to their Ethereum addresses. On another chain those
    # are somebody else's contracts, or nobody's, and quoting them measures
    # something other than the pair the user thinks they asked for.
    if chain_id != 1 and (args.token == WETH_MAINNET or args.numeraire == USDC_MAINNET):
        raise SystemExit(
            f"--token/--numeraire still hold their Ethereum defaults, but Fynd reports "
            f"chain {chain_id}. Pass the addresses for that chain."
        )
    token_meta = _token_override(args, "token")
    numeraire_meta = _token_override(args, "numeraire")
    unresolved = [
        address
        for address, meta in ((args.token, token_meta), (args.numeraire, numeraire_meta))
        if meta is None
    ]
    if unresolved:
        if tycho is None:
            raise SystemExit(
                "resolving "
                + ", ".join(unresolved)
                + " needs Tycho; describe them with --token-decimals/--token-symbol and "
                "--numeraire-decimals/--numeraire-symbol to measure without it."
            )
        resolved = resolve_tokens(tycho, unresolved)
        if token_meta is None:
            token_meta = resolved[args.token.lower()]
        if numeraire_meta is None:
            numeraire_meta = resolved[args.numeraire.lower()]
    if token_meta is None or numeraire_meta is None:
        raise SystemExit("could not determine token metadata for the pair.")

    # Every default below reads as dollars. Divide by the numeraire's dollar
    # price to say the same thing in numeraire units; a rate of None leaves them
    # as raw numeraire units, which is right only for a dollar numeraire.
    measured_rate = _numeraire_price_in_usd(args, chain_id, numeraire_meta, fynd, tycho)
    scale = 1.0 if measured_rate is None else 1.0 / measured_rate.rate
    defaults = SNAPSHOT_DEFAULTS
    return SnapshotConfig(
        token=token_meta,
        numeraire=numeraire_meta,
        pair=args.pair,
        chain_id=chain_id,
        search_min=args.search_min
        if args.search_min is not None
        else defaults["search_min"] * scale,
        search_max=args.search_max
        if args.search_max is not None
        else defaults["search_max"] * scale,
        samples_per_side=args.samples_per_side,
        probe_notional=defaults["probe_notional"] * scale,
        mid_band_min=ROBUST_MID_MIN_DEPTH * scale,
        mid_band_max=ROBUST_MID_MAX_DEPTH * scale,
        numeraire_usd=None if measured_rate is None else measured_rate.rate,
        numeraire_usd_block=None if measured_rate is None else measured_rate.block,
        numeraire_usd_spread_bps=(
            None if measured_rate is None else measured_rate.spread * 10_000.0
        ),
        slippage=slippage,
        max_workers=args.max_workers,
    )


def make_tycho(args: argparse.Namespace, chain_id: int) -> TychoClient:
    api_key = args.tycho_api_key or os.environ.get("TYCHO_API_KEY")
    if not api_key:
        raise SystemExit(
            "No Tycho API key: pass --tycho-api-key or set TYCHO_API_KEY "
            "(free key from https://t.me/fynd_portal_bot). Tycho is used only to look up "
            "token decimals and symbol — describe both tokens with --token-decimals/"
            "--token-symbol and --numeraire-decimals/--numeraire-symbol to measure "
            "without it."
        )
    chain_entry = CHAIN_TYCHO_HOSTS.get(chain_id)
    if chain_entry is None:
        raise SystemExit(
            f"Unknown chain_id {chain_id} from Fynd; supported: {sorted(CHAIN_TYCHO_HOSTS)}."
        )
    chain, default_tycho_url = chain_entry
    return TychoClient(args.tycho_url or default_tycho_url, api_key, chain=chain)


def run_snapshot(
    args: argparse.Namespace, fynd: FyndClient, tycho: TychoClient | None, chain_id: int
) -> int:
    config = build_config(args, chain_id, tycho, fynd)
    snapshot = collect_snapshot(fynd, config)
    print(json.dumps(snapshot.to_block_row(), indent=2))
    if args.write:
        rows_path, blocks_path, anchors_path = output_paths(args.out, config)
        written = append_jsonl(rows_path, snapshot.to_rows())
        append_jsonl(anchors_path, snapshot.to_anchor_rows(sender=fynd.sender))
        append_jsonl(blocks_path, [snapshot.to_block_row()])
        print(f"wrote {written} rows to {rows_path}", file=sys.stderr)
    return 0


def run_collect(
    args: argparse.Namespace, fynd: FyndClient, tycho: TychoClient | None, chain_id: int
) -> int:
    config = build_config(args, chain_id, tycho, fynd)
    poll_interval_s = (
        args.poll_interval_s
        if args.poll_interval_s is not None
        else CHAIN_POLL_INTERVAL_S.get(chain_id, DEFAULT_POLL_INTERVAL_S)
    )
    result = collect_blocks(
        fynd,
        config,
        out_dir=args.out,
        blocks=args.blocks,
        poll_interval_s=poll_interval_s,
    )
    print(
        f"recorded {result.blocks_recorded} blocks "
        f"({result.rows_written} rows, {result.anchors_written} anchors) "
        f"to {result.rows_path}; {result.idle_probes} idle probes, "
        f"{result.duplicate_snapshots} duplicate snapshots, "
        f"{result.failed_cycles} failed cycles",
        file=sys.stderr,
    )
    if result.interrupted:
        return 130  # standard shell convention for SIGINT
    return 0 if result.blocks_recorded > 0 else 1


def run_init_worker_pools(args: argparse.Namespace) -> int:
    out_path = Path(args.out)
    if out_path.exists() and not args.overwrite:
        raise SystemExit(f"{out_path} already exists; pass --overwrite to replace it.")
    packaged = importlib.resources.files("price_of_ethereum") / "data" / "worker_pools.toml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "init-worker-pools":
        return run_init_worker_pools(args)
    # Resolved before connecting: this rejects a half-described token, and Fynd
    # cold-start hydration can take minutes, which is a long time to wait to be
    # told a flag is missing its pair.
    tycho_required = _tycho_required(args)
    # Expected operational failures exit with a message, not a traceback.
    try:
        with FyndClient(args.fynd_url, sender=args.sender) as fynd:
            fynd.wait_until_ready(timeout_s=args.wait_ready_s)
            chain_id = fynd.info().chain_id
            # Skip Tycho (and its API key requirement) when both tokens are
            # fully described by --token-*/--numeraire-* overrides.
            tycho_context = (
                make_tycho(args, chain_id) if tycho_required else contextlib.nullcontext(None)
            )
            with tycho_context as tycho:
                if args.command == "snapshot":
                    return run_snapshot(args, fynd, tycho, chain_id)
                return run_collect(args, fynd, tycho, chain_id)
    except (
        CollectionAbortedError,
        SpotProbeError,
        FyndError,
        TychoError,
        LookupError,
        TimeoutError,
    ) as error:
        raise SystemExit(f"error: {error}") from error


if __name__ == "__main__":
    sys.exit(main())
