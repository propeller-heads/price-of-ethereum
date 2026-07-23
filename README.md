# price-of-ethereum

Regenerate the block-level onchain price-and-depth data behind
[marketprice.xyz](https://marketprice.xyz) — locally, from your own
[Fynd](https://docs.fynd.xyz) instance, for any token pair on any chain Fynd
supports. Every number is a measured Fynd quote or a simple function of measured
quotes. No oracles, no estimates — and nothing to trust but your own node.

> Status: early. Stage 0 scaffold. See the staged plan in the commit history.

## Why

marketprice.xyz shows "the real price of Ethereum" — the actual cost to trade
ETH at each block, with depth, routed across Tycho-indexed liquidity. This
package lets anyone reproduce that data themselves instead of trusting the
hosted site: point it at a local Fynd, sweep trade sizes across a block, and get
tidy per-rung price/impact/route data as a DataFrame.

## Install

```bash
pip install price-of-ethereum          # plain pip works; uv is optional
pip install "price-of-ethereum[viz]"   # + Plotly for the example notebooks

uv add price-of-ethereum               # if you prefer uv
```

## Run a local Fynd

The SDK talks to a Fynd server you run yourself. Get a free Tycho API key from
the Telegram bot [@fynd_portal_bot](https://t.me/fynd_portal_bot), then:

```bash
# Cargo
cargo install fynd
export TYCHO_API_KEY=<your-key>
fynd serve --chain ethereum --worker-pools-config worker_pools.toml

# Docker
docker run -e TYCHO_API_KEY=<your-key> -p 3000:3000 \
  -v "$PWD/worker_pools.toml:/worker_pools.toml" \
  ghcr.io/propeller-heads/fynd serve --chain ethereum \
  --worker-pools-config /worker_pools.toml
```

Fynd cold-start hydration takes ~1–5 min; the SDK's `wait_until_ready()` polls
`/v1/health` until it's serving. Ship the bundled [`worker_pools.toml`](./worker_pools.toml)
so the split (`path_frank_wolfe`) and baseline (`bellman_ford`) solvers both run,
matching the hosted deployment.

Other chains: `fynd serve --chain base` / `--chain unichain`, and point the SDK
at the matching Tycho host.

## Quickstart

```python
# Coming in Stage 1+. The shape:
from price_of_ethereum import FyndClient

fynd = FyndClient("http://127.0.0.1:3000")
fynd.wait_until_ready()
print(fynd.info().chain_id)
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
