# Example dataset

A real measurement, recorded by `poe collect` against a Fynd instance running on
Ethereum mainnet. Nothing here is simulated or hand-written.

| | |
| --- | --- |
| pair | `ETH/USDC` |
| chain id | 1 |
| tokens | `WETH` / `USDC` |
| blocks | 25,632,157 to 25,632,168 (12 of them) |
| block times | 2026-07-28 15:16:23 to 15:18:47 UTC |
| sizes per side | 100 |
| search range | 50 to 50,000,000 USDC |
| rows in the last block | 236 |
| mixed blocks | 0 |
| degraded mids | 0 |

The three files, and every field in them, are documented in the repository
README under "What lands on disk". This directory is the only place a recorded
dataset is committed; `.gitignore` keeps every other one out.

`examples/report.html` is rendered from exactly these files, and rebuilding it
needs neither Fynd nor Tycho. The committed copy differs only in carrying its
block range in the title:

```bash
poe report --out examples/data --output examples/report.html
```

Regenerate this note, the report and the README images with
`examples/build_showcase.py`.
