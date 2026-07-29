# Example dataset

A real measurement, recorded by `poe collect` against a Fynd instance running on
Ethereum mainnet. Nothing here is simulated or hand-written.

| | |
| --- | --- |
| pair | `ETH/USDC` |
| chain id | 1 |
| tokens | `WETH` / `USDC` |
| blocks | 25,638,023 to 25,638,034 (12 of them) |
| block times | 2026-07-29 10:54:47 to 10:56:59 UTC |
| sizes per side | 100 |
| search range | 50 to 50,000,000 USDC |
| rows in the last block | 236 |
| mixed blocks | 1 |
| degraded mids | 0 |

The three files, and every field in them, are documented in the repository
README under "What lands on disk". This directory is the only place a recorded
dataset is committed; `.gitignore` keeps every other one out.

`examples/quickstart.ipynb` reads these files and redraws every chart from them,
so the notebook needs neither Fynd nor Tycho to reproduce what is committed here.

Regenerate this note and the README image with `examples/build_showcase.py`.
