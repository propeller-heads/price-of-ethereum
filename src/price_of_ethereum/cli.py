"""`poe` — thin CLI over the library.

Two write-side commands measure (`snapshot`, `collect`) and need a local Fynd.
Token metadata (decimals, symbol) normally comes from Tycho — a key from
`--tycho-api-key` or the `TYCHO_API_KEY` environment variable (free key from
https://t.me/fynd_portal_bot) — but `--token-decimals`/`--token-symbol` and
`--numeraire-decimals`/`--numeraire-symbol` let a self-hosted Tycho or no-Tycho
user describe a token directly and skip that lookup for it.
One read-side command renders what is already on disk (`report`) and connects to
nothing; it needs the `viz` extra for Plotly.

All measurement logic lives in the library; the CLI only builds a
`SnapshotConfig` and drives it.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.resources
import json
import logging
import os
import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path

from price_of_ethereum import __version__
from price_of_ethereum.collect import (
    CollectionAbortedError,
    collect_blocks,
    output_paths,
    paths_for,
)
from price_of_ethereum.fynd.client import (
    DEFAULT_SLIPPAGE,
    DUMMY_SENDER,
    FyndClient,
    FyndError,
)
from price_of_ethereum.sizing import SpotProbeError
from price_of_ethereum.snapshot import SnapshotConfig, collect_snapshot
from price_of_ethereum.storage import append_jsonl, load_jsonl, load_latest_block_rows
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

# The sweep parameters have one source of truth: SnapshotConfig's defaults.
SNAPSHOT_DEFAULTS = {field.name: field.default for field in dataclass_fields(SnapshotConfig)}


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
            default=SNAPSHOT_DEFAULTS["search_min"],
            help="Smallest trade size to quote, in whole numeraire units (USDC by default).",
        )
        sub.add_argument(
            "--search-max",
            type=float,
            default=SNAPSHOT_DEFAULTS["search_max"],
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
            help="How long to wait for Fynd to finish cold-start hydration.",
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

    # The read-side command renders recorded data and connects to nothing, so it
    # locates files by pair/chain instead of asking Fynd which chain it serves.
    report_parser = subparsers.add_parser(
        "report", help="Write a self-contained HTML report (needs the viz extra)."
    )
    report_parser.add_argument("--out", default="data", help="Directory holding the JSONL files.")
    report_parser.add_argument(
        "--pair", default="ETH/USDC", help="Pair label used in the filenames."
    )
    report_parser.add_argument(
        "--chain-id", type=int, default=1, help="Chain id used in the filenames."
    )
    report_parser.add_argument(
        "--output", default="report.html", help="Path of the HTML file to write."
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


def build_config(
    args: argparse.Namespace, chain_id: int, tycho: TychoClient | None
) -> SnapshotConfig:
    # Argument-only checks first: they cost nothing and a run that is going to
    # fail should fail before it waits on Fynd or reaches for Tycho.
    slippage = _validated_slippage(args.slippage)
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
    return SnapshotConfig(
        token=token_meta,
        numeraire=numeraire_meta,
        pair=args.pair,
        chain_id=chain_id,
        search_min=args.search_min,
        search_max=args.search_max,
        samples_per_side=args.samples_per_side,
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
    config = build_config(args, chain_id, tycho)
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
    config = build_config(args, chain_id, tycho)
    result = collect_blocks(fynd, config, out_dir=args.out, blocks=args.blocks)
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


def read_side_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    rows_path, blocks_path, _anchors_path = paths_for(
        args.out, pair=args.pair, chain_id=args.chain_id
    )
    if not rows_path.exists() and not blocks_path.exists():
        raise SystemExit(
            f"no recorded data for {args.pair} on chain {args.chain_id} under {args.out}; "
            f"run `poe collect` first (expected {rows_path.name})."
        )
    return rows_path, blocks_path


def run_init_worker_pools(args: argparse.Namespace) -> int:
    out_path = Path(args.out)
    if out_path.exists() and not args.overwrite:
        raise SystemExit(f"{out_path} already exists; pass --overwrite to replace it.")
    packaged = importlib.resources.files("price_of_ethereum") / "data" / "worker_pools.toml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


def run_report(args: argparse.Namespace) -> int:
    import pandas as pd

    from price_of_ethereum.dashboard import write_report

    rows_path, blocks_path = read_side_paths(args)
    rows = load_latest_block_rows(rows_path) if rows_path.exists() else pd.DataFrame()
    blocks = load_jsonl(blocks_path) if blocks_path.exists() else pd.DataFrame()
    written = write_report(args.output, rows, blocks, title=f"{args.pair} — measured depth")
    print(f"wrote {written} ({written.stat().st_size / 1e6:.1f} MB, self-contained)")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "init-worker-pools":
        return run_init_worker_pools(args)
    # The read-side command renders what is on disk; it never reaches for Fynd.
    if args.command == "report":
        try:
            return run_report(args)
        except ImportError as error:
            raise SystemExit(
                "the report needs the viz extra: "
                "pip install 'price-of-ethereum[viz]' (or: uv sync --extra viz)"
            ) from error
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
