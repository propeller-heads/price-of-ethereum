# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - unreleased

First release, published as a GitHub release. This package is not distributed
on a package index, so there is no upgrade path to describe.

### Added

- `FyndClient` and `TychoClient`, covering the wire behaviour the OpenAPI specs
  do not capture — notably that Fynd wants `slippage` as a decimal string, and
  that a 200 response can still carry a per-order failure.
- `collect_snapshot` and `collect_blocks`, recording block-level price and depth
  by sweeping ~100 trade sizes per side across a single block.
- Anchored levels persist the executable proof of each headline measurement to
  `*.anchors.jsonl`: order id, transaction, fee breakdown and a Tenderly
  simulation URL. Encoding is requested only for those quotes.
- `--sender` sets the address quotes are encoded for. It defaults to a
  placeholder that holds nothing, and a link built for it is recorded as
  `placeholder_sender` rather than `ready`, since it cannot simulate the trade.
- `poe` CLI: `snapshot`, `collect`, `report` and `init-worker-pools`.
- `poe report` writes a self-contained HTML file with `plotly.io.to_html`. It
  contains no JavaScript written in this repository.
- JSONL storage, with Parquet conversion behind the `parquet` extra.
- `--token-decimals`, `--token-symbol`, `--numeraire-decimals` and
  `--numeraire-symbol` describe tokens without calling Tycho. Supply all four
  and no Tycho API key is needed.
- Hosted Tycho endpoints for every chain Fynd routes on: ethereum (chain id 1),
  bsc (56), unichain (130), polygon (137), base (8453) and arbitrum (42161).
- A measurement regression test reproducing the production marketprice.xyz
  method bit-for-bit against a fixed AMM fixture.

### Notes

- Verified against live Ethereum mainnet liquidity: 30+ blocks collected with
  Fynd 0.97.4. The other five chains are wired but untested.
- `pandas` and `pyarrow` are not installed by default. Use the `data` extra for
  storage and reporting, or `viz` for the charts.
- Response types that upstream controls are deliberately open: a Fynd quote
  status or Tycho chain this SDK has never seen parses normally instead of
  raising.
