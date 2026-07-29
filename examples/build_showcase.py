"""Regenerate the example artifacts committed to this repository.

Reads the recorded dataset in `examples/data` — a real Ethereum mainnet
measurement — and writes the image `README.md` links to and a provenance note
beside the dataset. It then executes `examples/quickstart.ipynb` and injects a
static image into each figure, because GitHub's notebook viewer runs no
JavaScript and a Plotly figure alone is a blank gap there.

Only the notebook step needs a running Fynd; everything else reads what is
already on disk. To record a fresh dataset first:

    poe collect --blocks 12 --out examples/data

Then, with a Chrome for kaleido:

    uv run --with nbconvert --with ipykernel --with kaleido \
        python examples/build_showcase.py

Those three are named here rather than declared as a dependency group. They are
needed only to rebuild these artifacts, and pinning them would carry a compiled
JSON extension and a C parser in the lockfile of a package that never imports
either.
"""

from __future__ import annotations

import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from price_of_ethereum.charts import cost_curve_figure
from price_of_ethereum.storage import load_jsonl, load_latest_block_rows

EXAMPLES = Path(__file__).parent
DATA = EXAMPLES / "data"
IMAGES = EXAMPLES / "images"
NOTEBOOK = EXAMPLES / "quickstart.ipynb"
DATASET_NOTE = DATA / "README.md"

PLOTLY_MIME = "application/vnd.plotly.v1+json"
# GitHub's notebook viewer runs no JavaScript, so a figure that ships only the
# Plotly mimetype renders as an empty gap there. A PNG beside it gives GitHub
# something to draw; JupyterLab and nbviewer still pick the interactive output.
# Scale is held down deliberately: GitHub stops rendering a blob past a size
# limit, and at scale 2 these images push the notebook to a megabyte, which
# would cost the file the very thing they are here to give it.
PNG_WIDTH, PNG_HEIGHT, PNG_SCALE = 1000, 450, 1.5


def load_dataset() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(latest block's rows, every block summary) from the committed dataset.

    The per-size charts show one block, the
    history charts show every recorded one.
    """
    summaries = sorted(DATA.glob("*.blocks.jsonl"))
    if len(summaries) != 1:
        raise SystemExit(
            f"expected exactly one *.blocks.jsonl in {DATA}, found {len(summaries)}. "
            "Record one with: poe collect --blocks 12 --out examples/data"
        )
    blocks_path = summaries[0]
    rows_path = blocks_path.with_name(blocks_path.name.replace(".blocks.", ".rows."))
    return load_latest_block_rows(rows_path), load_jsonl(blocks_path)


def provenance(blocks: pd.DataFrame) -> str:
    """What this dataset is, in one line: pair, chain, blocks, date.

    Read off the measurement itself rather than off the clock, so it stays true
    however long after collection the artifacts are rebuilt.
    """
    latest = blocks.sort_values("block_number").iloc[-1]
    day = datetime.fromtimestamp(int(blocks["block_timestamp"].min()), UTC)
    return (
        f"{latest['pair']} on chain {latest['chain_id']} — {len(blocks)} blocks, "
        f"{blocks['block_number'].min():,} to {blocks['block_number'].max():,}, "
        f"{day:%Y-%m-%d} UTC"
    )


def write_dataset_note(blocks: pd.DataFrame, rows: pd.DataFrame) -> None:
    """A README beside the JSONL saying exactly what was measured, and when."""
    latest = blocks.sort_values("block_number").iloc[-1]
    first_time = datetime.fromtimestamp(int(blocks["block_timestamp"].min()), UTC)
    last_time = datetime.fromtimestamp(int(blocks["block_timestamp"].max()), UTC)
    degraded = int((blocks["mid_source"] != "sweep_band").sum())
    block_range = f"{blocks['block_number'].min():,} to {blocks['block_number'].max():,}"
    search_range = f"{latest['search_min']:,.0f} to {latest['search_max']:,.0f}"
    DATASET_NOTE.write_text(
        f"""# Example dataset

A real measurement, recorded by `poe collect` against a Fynd instance running on
Ethereum mainnet. Nothing here is simulated or hand-written.

| | |
| --- | --- |
| pair | `{latest["pair"]}` |
| chain id | {latest["chain_id"]} |
| tokens | `{latest["token_symbol"]}` / `{latest["numeraire_symbol"]}` |
| blocks | {block_range} ({len(blocks)} of them) |
| block times | {first_time:%Y-%m-%d %H:%M:%S} to {last_time:%H:%M:%S} UTC |
| sizes per side | {int(latest["samples_per_side"])} |
| search range | {search_range} {latest["numeraire_symbol"]} |
| rows in the last block | {len(rows):,} |
| mixed blocks | {int(blocks["mixed_block"].sum())} |
| degraded mids | {degraded} |

The three files, and every field in them, are documented in the repository
README under "What lands on disk". This directory is the only place a recorded
dataset is committed; `.gitignore` keeps every other one out.

`examples/quickstart.ipynb` reads these files and redraws every chart from them,
so the notebook needs neither Fynd nor Tycho to reproduce what is committed here.

Regenerate this note and the README image with `examples/build_showcase.py`.
""",
        encoding="utf-8",
    )


def execute_notebook() -> None:
    """Run every cell against whatever Fynd is listening, in place.

    With nothing listening the notebook falls back to `simulated_fynd.py` and
    still runs, but its stored outputs are then fabricated and must not be
    committed — `tests/test_examples.py` fails if they are.
    """
    subprocess.run(
        [
            "jupyter",
            "nbconvert",
            "--to",
            "notebook",
            "--execute",
            "--inplace",
            "--ExecutePreprocessor.timeout=1200",
            NOTEBOOK.name,
        ],
        cwd=EXAMPLES,
        check=True,
    )


def inject_static_images(path: Path) -> int:
    """Add an `image/png` beside every Plotly output in an executed notebook."""
    notebook = json.loads(path.read_text(encoding="utf-8"))
    injected = 0
    for cell in notebook["cells"]:
        for output in cell.get("outputs", []):
            data = output.get("data", {})
            if PLOTLY_MIME not in data:
                continue
            payload = data[PLOTLY_MIME]
            figure = go.Figure(data=payload["data"], layout=payload["layout"])
            image = figure.to_image(
                format="png", width=PNG_WIDTH, height=PNG_HEIGHT, scale=PNG_SCALE
            )
            data["image/png"] = base64.b64encode(image).decode("ascii")
            output.setdefault("metadata", {})["image/png"] = {
                "width": PNG_WIDTH,
                "height": PNG_HEIGHT,
            }
            injected += 1
    path.write_text(json.dumps(notebook, indent=1) + "\n", encoding="utf-8")
    return injected


def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    rows, blocks = load_dataset()
    label = provenance(blocks)
    print(f"dataset: {label}")

    write_dataset_note(blocks, rows)
    print(f"wrote {DATASET_NOTE}")

    latest = blocks.sort_values("block_number").iloc[-1]
    curve = cost_curve_figure(rows, numeraire_symbol=str(latest["numeraire_symbol"]))
    curve.update_layout(
        title={"text": f"Cost curve — {latest['pair']}, block {int(latest['block_number']):,}"}
    )
    curve.write_image(IMAGES / "cost-curve.png", width=1200, height=560, scale=2)
    print(f"wrote {IMAGES / 'cost-curve.png'}")

    execute_notebook()
    print(f"executed {NOTEBOOK}, injected {inject_static_images(NOTEBOOK)} static images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
