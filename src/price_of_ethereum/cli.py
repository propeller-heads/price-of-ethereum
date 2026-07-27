"""`poe` — thin CLI over the library.

Two write-side commands measure (`snapshot`, `collect`) and need a local Fynd
plus a Tycho key for token metadata — from `--tycho-api-key` or the
`TYCHO_API_KEY` environment variable (free key from https://t.me/fynd_portal_bot).
Two read-side commands render what is already on disk (`serve`, `report`) and
connect to nothing; they need the `viz` extra for Plotly.

All measurement logic lives in the library; the CLI only builds a
`SnapshotConfig` and drives it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pandas as pd

from price_of_ethereum.collect import (
    CollectionAbortedError,
    collect_blocks,
    output_paths,
    paths_for,
)
from price_of_ethereum.fynd.client import FyndClient, FyndError
from price_of_ethereum.serve import DEFAULT_HOST, DEFAULT_POLL_S, DEFAULT_PORT, serve_dashboard
from price_of_ethereum.sizing import SpotProbeError
from price_of_ethereum.snapshot import SnapshotConfig, collect_snapshot
from price_of_ethereum.storage import append_jsonl, load_jsonl, load_latest_block_rows
from price_of_ethereum.tokens import resolve_tokens
from price_of_ethereum.tycho.client import TychoClient, TychoError
from price_of_ethereum.tycho.models import Chain

WETH_MAINNET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC_MAINNET = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

DEFAULT_FYND_URL = "http://127.0.0.1:3000"

# Fynd's /v1/info reports a numeric chain id; Tycho wants a chain name, and the
# hosted Tycho deployment runs one host per chain.
CHAIN_TYCHO_HOSTS: dict[int, tuple[Chain, str]] = {
    1: ("ethereum", "https://tycho-beta.propellerheads.xyz"),
    130: ("unichain", "https://tycho-unichain-beta.propellerheads.xyz"),
    8453: ("base", "https://tycho-base-beta.propellerheads.xyz"),
}

# The sweep parameters have one source of truth: SnapshotConfig's defaults.
SNAPSHOT_DEFAULTS = {field.name: field.default for field in dataclass_fields(SnapshotConfig)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poe",
        description="Measure block-level onchain price and depth from your own local Fynd.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("snapshot", "Collect one block snapshot and print its summary."),
        ("collect", "Record snapshots block by block into JSONL files."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--fynd-url", default=DEFAULT_FYND_URL)
        sub.add_argument(
            "--tycho-url",
            default=None,
            help="Defaults to the hosted Tycho for the chain Fynd reports.",
        )
        sub.add_argument(
            "--tycho-api-key",
            default=None,
            help="Defaults to the TYCHO_API_KEY environment variable.",
        )
        sub.add_argument("--token", default=WETH_MAINNET, help="Traded token address.")
        sub.add_argument("--numeraire", default=USDC_MAINNET, help="Numeraire token address.")
        sub.add_argument("--pair", default="ETH/USDC", help="Label stored on every row.")
        sub.add_argument(
            "--samples-per-side", type=int, default=SNAPSHOT_DEFAULTS["samples_per_side"]
        )
        sub.add_argument("--search-min", type=float, default=SNAPSHOT_DEFAULTS["search_min"])
        sub.add_argument("--search-max", type=float, default=SNAPSHOT_DEFAULTS["search_max"])
        sub.add_argument("--max-workers", type=int, default=SNAPSHOT_DEFAULTS["max_workers"])
        sub.add_argument(
            "--wait-ready-s",
            type=float,
            default=300.0,
            help="How long to wait for Fynd to finish cold-start hydration.",
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

    # Read-side commands render recorded data and connect to nothing, so they
    # locate files by pair/chain instead of asking Fynd which chain it serves.
    for name, help_text in (
        ("serve", "Serve a live dashboard over recorded data (needs the viz extra)."),
        ("report", "Write a self-contained HTML report (needs the viz extra)."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--out", default="data", help="Directory holding the JSONL files.")
        sub.add_argument("--pair", default="ETH/USDC", help="Pair label used in the filenames.")
        sub.add_argument("--chain-id", type=int, default=1, help="Chain id used in the filenames.")

    serve_parser = subparsers.choices["serve"]
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument(
        "--poll-s", type=float, default=DEFAULT_POLL_S, help="Page refresh interval in seconds."
    )
    report_parser = subparsers.choices["report"]
    report_parser.add_argument(
        "--output", default="report.html", help="Path of the HTML file to write."
    )
    return parser


def build_config(args: argparse.Namespace, chain_id: int, tycho: TychoClient) -> SnapshotConfig:
    resolved = resolve_tokens(tycho, [args.token, args.numeraire])
    return SnapshotConfig(
        token=resolved[args.token.lower()],
        numeraire=resolved[args.numeraire.lower()],
        pair=args.pair,
        chain_id=chain_id,
        search_min=args.search_min,
        search_max=args.search_max,
        samples_per_side=args.samples_per_side,
        max_workers=args.max_workers,
    )


def make_tycho(args: argparse.Namespace, chain_id: int) -> TychoClient:
    api_key = args.tycho_api_key or os.environ.get("TYCHO_API_KEY")
    if not api_key:
        raise SystemExit(
            "No Tycho API key: pass --tycho-api-key or set TYCHO_API_KEY "
            "(free key from https://t.me/fynd_portal_bot)."
        )
    chain_entry = CHAIN_TYCHO_HOSTS.get(chain_id)
    if chain_entry is None:
        raise SystemExit(
            f"Unknown chain_id {chain_id} from Fynd; supported: {sorted(CHAIN_TYCHO_HOSTS)}."
        )
    chain, default_tycho_url = chain_entry
    return TychoClient(args.tycho_url or default_tycho_url, api_key, chain=chain)


def run_snapshot(
    args: argparse.Namespace, fynd: FyndClient, tycho: TychoClient, chain_id: int
) -> int:
    config = build_config(args, chain_id, tycho)
    snapshot = collect_snapshot(fynd, config)
    print(json.dumps(snapshot.to_block_row(), indent=2))
    if args.write:
        rows_path, blocks_path = output_paths(args.out, config)
        written = append_jsonl(rows_path, snapshot.to_rows())
        append_jsonl(blocks_path, [snapshot.to_block_row()])
        print(f"wrote {written} rows to {rows_path}", file=sys.stderr)
    return 0


def run_collect(
    args: argparse.Namespace, fynd: FyndClient, tycho: TychoClient, chain_id: int
) -> int:
    config = build_config(args, chain_id, tycho)
    result = collect_blocks(fynd, config, out_dir=args.out, blocks=args.blocks)
    print(
        f"recorded {result.blocks_recorded} blocks ({result.rows_written} rows) "
        f"to {result.rows_path}; {result.idle_probes} idle probes, "
        f"{result.duplicate_snapshots} duplicate snapshots, "
        f"{result.failed_cycles} failed cycles",
        file=sys.stderr,
    )
    if result.interrupted:
        return 130  # standard shell convention for SIGINT
    return 0 if result.blocks_recorded > 0 else 1


def read_side_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    rows_path, blocks_path = paths_for(args.out, pair=args.pair, chain_id=args.chain_id)
    if not rows_path.exists() and not blocks_path.exists():
        raise SystemExit(
            f"no recorded data for {args.pair} on chain {args.chain_id} under {args.out}; "
            f"run `poe collect` first (expected {rows_path.name})."
        )
    return rows_path, blocks_path


def run_serve(args: argparse.Namespace) -> int:
    rows_path, blocks_path = read_side_paths(args)
    serve_dashboard(
        rows_path,
        blocks_path,
        title=f"{args.pair} — measured depth",
        host=args.host,
        port=args.port,
        poll_s=args.poll_s,
    )
    return 0


def run_report(args: argparse.Namespace) -> int:
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
    # Read-side commands render what is on disk; they never reach for Fynd.
    if args.command in ("serve", "report"):
        try:
            return run_serve(args) if args.command == "serve" else run_report(args)
        except ImportError as error:
            raise SystemExit(
                "the dashboard needs the viz extra: "
                "pip install 'price-of-ethereum[viz]' (or: uv sync --extra viz)"
            ) from error
    # Expected operational failures exit with a message, not a traceback.
    try:
        with FyndClient(args.fynd_url) as fynd:
            fynd.wait_until_ready(timeout_s=args.wait_ready_s)
            chain_id = fynd.info().chain_id
            with make_tycho(args, chain_id) as tycho:
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
