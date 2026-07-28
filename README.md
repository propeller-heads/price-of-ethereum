# Price Of Ethereum SDK

What does it actually cost to trade a token pair onchain, right now, at size?
This measures it — block by block, from your own [Fynd](https://docs.fynd.xyz)
instance, for any pair on any chain Fynd supports. Every number is a Fynd quote
or a simple function of quotes. No oracles and no price estimates; the one
hosted dependency is Tycho, which resolves a token's decimals, symbol, quality
and tax before measuring (Fynd needs a Tycho API key regardless, to index the
liquidity it quotes against). Pass `--token-decimals`, `--token-symbol`,
`--numeraire-decimals` and `--numeraire-symbol` to skip even that lookup.

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

This package is not distributed on a package index. Install it from the
repository, or pin a tag from the
[releases](https://github.com/propeller-heads/price-of-ethereum/releases):

```bash
pip install "git+https://github.com/propeller-heads/price-of-ethereum"
pip install "price-of-ethereum[viz] @ git+https://github.com/propeller-heads/price-of-ethereum"
pip install "git+https://github.com/propeller-heads/price-of-ethereum@v0.1.0"
```

Measuring needs nothing heavy — the base install is `httpx` and `pydantic`, and
`poe snapshot` and `poe collect` run on it. The extras are additive:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `data` | pandas | reading recorded JSONL back as a DataFrame |
| `viz` | pandas, Plotly | `poe report` and the notebook |
| `parquet` | pandas, pyarrow | `to_parquet` / `load_parquet` |

`parquet` is separate because pyarrow is a ~123 MB install, which is a lot to
impose on someone who only wants a chart. uv works too (`uv add "git+…"`) but
nothing here requires it.

## Setup

The SDK measures against a [Fynd](https://docs.fynd.xyz) server you run
yourself. Five steps, start to finish.

**1. Get a Tycho API key.** Message the Telegram bot
[@fynd_portal_bot](https://t.me/fynd_portal_bot). The key is required, not
optional: Fynd uses it to index the liquidity it quotes against. Both the
[Fynd quickstart](https://docs.fynd.xyz/get-started/quickstart) and the
[Tycho docs](https://docs.propellerheads.xyz/tycho) point at the same bot.

**2. Install Fynd.**

```bash
cargo install fynd
```

**3. Write the worker-pool config.**

```bash
poe init-worker-pools     # writes ./worker_pools.toml (--out to change the path)
```

Fynd's `-w` / `--worker-pools-config` defaults to `worker_pools.toml` in the
working directory, so starting Fynd from the same directory picks this file up
with no flag. It configures three of Fynd's four documented solver algorithms:
`bellman_ford` (the single-path baseline the bulk sweep waits on), plus
`path_frank_wolfe` and `water_fill` for the split routes the anchored levels
wait on. See
[server configuration](https://docs.fynd.xyz/guides/server-configuration) for
every knob; the file itself explains why each value was chosen.

**4. Start Fynd.**

```bash
export TYCHO_API_KEY=<your-key>
export RUST_LOG=fynd=info
fynd serve                          # add -w /path/to/worker_pools.toml if it is elsewhere
```

Or with Docker — port 3000 is the API, 9898 the Prometheus metrics endpoint:

```bash
docker run -e TYCHO_API_KEY=<your-key> -e RUST_LOG=fynd=info \
  -p 3000:3000 -p 9898:9898 \
  -v "$PWD/worker_pools.toml:/worker_pools.toml" \
  ghcr.io/propeller-heads/fynd serve -w /worker_pools.toml
```

**5. Wait for hydration, then measure.** Cold start takes ~1–5 min while Fynd
loads protocol state. Check it directly:

```bash
curl http://localhost:3000/v1/health
```

`wait_until_ready()` polls that same endpoint, and `poe snapshot` and
`poe collect` both call it before measuring, so you can simply start them and
wait.

### Chains

`fynd serve --chain` defaults to `ethereum` and also accepts `base`, `unichain`,
`bsc`, `arbitrum` and `polygon` — see the
[server configuration docs](https://docs.fynd.xyz/guides/server-configuration)
for the current list.

This SDK ships a hosted Tycho endpoint for all six, so selecting a chain is one
flag on Fynd and nothing here:

| `fynd serve --chain` | chain id | Tycho host                                    |
| -------------------- | -------- | --------------------------------------------- |
| `ethereum`           | 1        | `tycho-beta.propellerheads.xyz`               |
| `bsc`                | 56       | `tycho-bsc-beta.propellerheads.xyz`           |
| `unichain`           | 130      | `tycho-unichain-beta.propellerheads.xyz`      |
| `polygon`            | 137      | `tycho-polygon-beta.propellerheads.xyz`       |
| `base`               | 8453     | `tycho-base-beta.propellerheads.xyz`          |
| `arbitrum`           | 42161    | `tycho-arbitrum-beta.propellerheads.xyz`      |

`poe` reads the chain id from Fynd's `/v1/info` and picks the matching host, so
you never pass it twice. **Only Ethereum mainnet has been verified end to end
against live liquidity.** The other five are wired up but untested — the host
answers, and nothing beyond that has been checked.

Point `poe --tycho-url` at a self-hosted or otherwise different Tycho to
override the host for the chain Fynd reports. To skip Tycho altogether — on a
chain not in this table, or with no key at all — pass `--token-decimals`,
`--token-symbol`, `--numeraire-decimals` and `--numeraire-symbol` and `poe`
measures against your local Fynd alone.

## Quickstart

```bash
export TYCHO_API_KEY=<your-key>

poe snapshot                       # measure one block, print its summary
poe collect --blocks 50 --out data # record 50 blocks into JSONL
poe report --out data --output report.html   # self-contained HTML report
```

`poe snapshot --help` lists the measurement knobs (`--pair`, `--token`,
`--numeraire`, `--samples-per-side`, `--search-min/max`). `report` reads only
what is already on disk — it never contacts Fynd or Tycho. Fynd's HTTP surface,
if you want to call it directly, is documented in the
[API reference](https://docs.fynd.xyz/reference/api).

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

Each anchored level also writes the transaction behind it to `*.anchors.jsonl`:
the router address, the calldata, the fee breakdown, and a Tenderly simulation
link — the proof that a measurement corresponds to a trade someone could send.
Quotes are encoded for `--sender`, which defaults to a placeholder address that
holds nothing, so those links open but revert on the transfer and are recorded
as `tenderly_status="placeholder_sender"`. Pass an address that actually holds
the sell token and has approved the router to get `ready` links that simulate.

## Report

`poe report` renders the recorded JSONL into one HTML file: cost curve, book map
with the robust-mid band shaded, round-trip spread as a percent of the mid, the
anchored levels as a table, and mid/depth/latency across every recorded block.
Axes carry the real token symbols, and prices read numeraire per token
throughout. Plotly ships inside the `viz` extra and its bundle is inlined, so
the file is ~5 MB, opens straight from disk, and requests nothing off-machine.

The report is a snapshot of the file at the moment you ran it. To watch a
running collection, re-run `poe report` and reopen the file — or, if you want it
over HTTP, serve the directory:

```bash
poe collect --out data &
poe report --out data --output report.html
python -m http.server --directory .    # optional; the file works fine without it
```

The JSONL the collector writes is the full raw dataset, so nothing in the report
is the only copy of anything.

## Notebook

[`examples/quickstart.ipynb`](./examples/quickstart.ipynb) walks the whole path:
connect, one snapshot, what a measurement contains, the charts, where impact
stops being monotonic, and recording a history. It calls the same figure
builders `poe report` does, so it draws exactly what the report draws — use the
notebook to explore interactively, the report to share a result.

```bash
pip install jupyterlab            # alongside the viz extra above
jupyter lab examples/quickstart.ipynb
```

## Development

See [CONTRIBUTING.md](./CONTRIBUTING.md) for dev setup, checks, and commit
conventions. With uv (recommended):

```bash
uv sync --all-extras --dev
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```

Or with plain pip — uv, ruff, and ty are development conveniences, never runtime
requirements:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[data,viz]" --group dev   # pip >= 25.1; or: pip install -e . pytest
pytest
```

## License

MIT — see [LICENSE](./LICENSE).
