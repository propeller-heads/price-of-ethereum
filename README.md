# price-of-ethereum

What does it actually cost to trade a token pair onchain, right now, at size?
This measures it — block by block, from your own [Fynd](https://docs.fynd.xyz)
instance, for any pair on any chain Fynd supports. Every number is a Fynd quote
or a simple function of quotes. No oracles, no estimates, and nothing to trust
but your own node.

> Status: verified against Ethereum mainnet. Treat the API as unstable until 1.0.

## Why

A single "price" says nothing about what you can trade. The price to move $1,000
and the price to move $5,000,000 differ by orders of magnitude, and both change
every block as liquidity moves.

So instead of quoting one number, this sweeps ~100 trade sizes per side across a
single block and records what the router actually returns at each one: execution
price, price impact, the pools routed through, and gas. The result is the shape
of the market — a cost curve and a two-sided book — rather than a point estimate,
and it arrives as a tidy DataFrame you can check yourself.

## Install

Not on PyPI yet — install from the repository:

```bash
pip install "git+https://github.com/propeller-heads/price-of-ethereum"
pip install "price-of-ethereum[viz] @ git+https://github.com/propeller-heads/price-of-ethereum"
```

The `viz` extra adds Plotly, needed for the dashboard and the notebook. uv works
too (`uv add "git+…"`) but nothing here requires it.

## Run a local Fynd

The SDK talks to a Fynd server you run yourself. Get a free Tycho API key from
the Telegram bot [@fynd_portal_bot](https://t.me/fynd_portal_bot), then:

```bash
# Cargo
cargo install fynd
export TYCHO_API_KEY=<your-key>
curl -O https://raw.githubusercontent.com/propeller-heads/price-of-ethereum/main/worker_pools.toml
fynd serve --chain ethereum --worker-pools-config worker_pools.toml

# Docker
docker run -e TYCHO_API_KEY=<your-key> -p 3000:3000 \
  -v "$PWD/worker_pools.toml:/worker_pools.toml" \
  ghcr.io/propeller-heads/fynd serve --chain ethereum \
  --worker-pools-config /worker_pools.toml
```

Cold-start hydration takes ~1–5 min; `wait_until_ready()` polls `/v1/health`
until Fynd is serving, and both CLI commands call it before measuring. Pass the
bundled [`worker_pools.toml`](./worker_pools.toml) so all three solvers run — the
`bellman_ford` baseline the bulk sweep waits on, plus `path_frank_wolfe` and
`water_fill` for the split routes the anchored levels wait on.

Other chains: `fynd serve --chain base` / `--chain unichain`, and point the SDK
at the matching Tycho host.

## Quickstart

```bash
export TYCHO_API_KEY=<your-key>

poe snapshot                       # measure one block, print its summary
poe collect --blocks 50 --out data # record 50 blocks into JSONL
poe serve  --out data              # live dashboard on http://127.0.0.1:8765
poe report --out data --output report.html   # frozen, self-contained copy
```

`poe snapshot --help` lists the measurement knobs (`--pair`, `--token`,
`--numeraire`, `--samples-per-side`, `--search-min/max`). `serve` and `report`
read only what is already on disk — they never contact Fynd or Tycho.

As a library:

```python
import os

from price_of_ethereum import (
    FyndClient, SnapshotConfig, TychoClient, collect_snapshot, resolve_tokens,
)

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

fynd = FyndClient("http://127.0.0.1:3000")
fynd.wait_until_ready()
tycho = TychoClient("https://tycho-beta.propellerheads.xyz", os.environ["TYCHO_API_KEY"])
tokens = resolve_tokens(tycho, [WETH, USDC])

snapshot = collect_snapshot(fynd, SnapshotConfig(
    token=tokens[WETH.lower()],
    numeraire=tokens[USDC.lower()],
    pair="ETH/USDC",
    chain_id=fynd.info().chain_id,
))
print(snapshot.robust_mid, snapshot.median_depth, snapshot.mid_source)
rows = snapshot.to_rows()   # one dict per measured rung, curve + anchor
```

## What you get per block

`spot` from a single $1,000 probe; `robust_mid` as the median of two-sided
midpoints in the $2,500–$10,000 numeraire band (with dedicated-probe and
spot fallbacks, reported in `mid_source`); a log-spaced cost curve on both
sides; anchored measurements at the headline impact levels, solved waiting for
every solver pool so split routes are captured; and per-rung route metadata.
Block identity comes from the quotes themselves — the majority block labels the
snapshot and `mixed_block` flags a straddle. There is no RPC client and no
database anywhere in this package.

## Dashboard

`poe serve` renders the recorded JSONL and refreshes as new blocks land: cost
curve, book map with the robust-mid band shaded, round-trip spread as a percent of
the mid, the anchored level table, and mid/depth/latency across blocks. Axes carry
the real token symbols, and one button flips the whole view to the other side of
the pair. Plotly ships inside the `viz` extra, so the page loads no external
scripts and works offline.

Run the collector and the dashboard side by side:

```bash
poe collect --out data &
poe serve --out data
```

## Notebook

[`examples/quickstart.ipynb`](./examples/quickstart.ipynb) walks the whole path:
connect, one snapshot, what a measurement contains, the charts, where impact
stops being monotonic, and recording a history. It calls the same figure
builders the dashboard uses, so it draws exactly what `poe serve` draws — use
the notebook to understand the data, the dashboard to watch it.

```bash
pip install jupyterlab            # alongside the viz extra above
jupyter lab examples/quickstart.ipynb
```

## Development

With uv (recommended):

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```

Or with plain pip — uv, ruff, and ty are development conveniences, never runtime
requirements:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e . --group dev   # pip >= 25.1; or: pip install -e . pytest
pytest
```

## License

MIT
