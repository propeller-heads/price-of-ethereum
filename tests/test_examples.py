"""The committed examples say things about themselves that have to stay true.

The notebook's stored outputs claim to be a mainnet measurement, the dataset in
`examples/data` claims to be one pair on one chain at one resolution, and the
notebook's figures claim to be visible on GitHub. Each is easy to break from a
distance: executing the notebook with no Fynd running silently swaps in the
simulator, collecting into `examples/data` mixes two sweeps into one history,
and re-executing without `build_showcase.py` drops every static image.

`examples/simulated_fynd.py` — the offline path that makes the notebook runnable
without a server at all — is covered here too.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from price_of_ethereum import SnapshotConfig, collect_snapshot
from price_of_ethereum.collect import collect_blocks

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES))

import simulated_fynd  # noqa: E402 - resolvable only after the path insert above

NOTEBOOK = EXAMPLES / "quickstart.ipynb"
DATASET_BLOCKS = EXAMPLES / "data" / "eth-usdc_1.blocks.jsonl"
PLOTLY_MIME = "application/vnd.plotly.v1+json"


def simulated_config() -> SnapshotConfig:
    return SnapshotConfig(
        token=simulated_fynd.SIM_TOKEN,
        numeraire=simulated_fynd.SIM_NUMERAIRE,
        pair=simulated_fynd.SIM_PAIR,
        chain_id=simulated_fynd.SIM_CHAIN_ID,
        search_min=simulated_fynd.SIM_SEARCH_MIN,
        search_max=simulated_fynd.SIM_SEARCH_MAX,
        samples_per_side=12,
        impact_levels=(1.0,),
        anchor_targets=(1.0,),
        max_workers=2,
    )


def test_a_simulated_snapshot_is_healthy() -> None:
    # A degraded mid would leave the notebook's book map without its band and
    # the history charts without a line, which is not what a reader should meet.
    with simulated_fynd.simulated_fynd() as fynd:
        snapshot = collect_snapshot(fynd, simulated_config())
    assert snapshot.mid_source == "sweep_band"
    assert snapshot.mixed_block is False
    assert snapshot.robust_mid == pytest.approx(2500.0, rel=0.02)
    rows = snapshot.to_rows()
    assert {row["side"] for row in rows} == {"buy", "sell"}
    assert {row["kind"] for row in rows} == {"curve", "anchor"}


def test_the_simulated_chain_advances_one_block_per_collect_cycle() -> None:
    # The simulator moves its block on the first request after a quiet gap, so a
    # sweep sees one block and the collector's idle probe is what advances it.
    # The poll interval here only has to clear that gap.
    config = simulated_config()
    with simulated_fynd.simulated_fynd() as fynd, tempfile.TemporaryDirectory() as out_dir:
        result = collect_blocks(fynd, config, out_dir=out_dir, blocks=3, poll_interval_s=1.2)
        summaries = [
            json.loads(line) for line in result.blocks_path.read_text(encoding="utf-8").splitlines()
        ]
    assert result.blocks_recorded == 3
    assert result.failed_cycles == 0
    assert result.duplicate_snapshots == 0
    numbers = [summary["block_number"] for summary in summaries]
    assert numbers == list(range(numbers[0], numbers[0] + 3))
    assert not any(summary["mixed_block"] for summary in summaries)


def test_the_notebook_ships_a_static_image_for_every_figure() -> None:
    # GitHub's notebook viewer executes no JavaScript, so an output carrying
    # only the Plotly mimetype is a blank gap there. `examples/build_showcase.py`
    # injects a PNG beside each one; re-executing the notebook without that step
    # drops them silently and the README's whole point goes with them.
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    figures = [
        output
        for cell in notebook["cells"]
        for output in cell.get("outputs", [])
        if PLOTLY_MIME in output.get("data", {})
    ]
    assert figures, "the committed notebook has no rendered figures"
    assert all("image/png" in output["data"] for output in figures)


def test_the_notebook_stored_a_real_measurement() -> None:
    # Its header tells the reader the stored outputs are a mainnet measurement.
    # Executing with nothing on the Fynd port falls back to the simulator and
    # succeeds, so committing that run is the easy way to turn the header into a
    # false claim; the simulated pair name is the fingerprint of it having
    # happened.
    assert simulated_fynd.SIM_PAIR not in NOTEBOOK.read_text(encoding="utf-8")


def dataset_summaries() -> list[dict]:
    return [
        json.loads(line)
        for line in DATASET_BLOCKS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_the_committed_dataset_is_one_pair_at_one_resolution() -> None:
    # `poe collect` appends, so pointing a second collection at this directory
    # would splice a different pair, chain or sweep width into the same history
    # and the report built from it would silently plot two things at once.
    summaries = dataset_summaries()
    assert len(summaries) >= 2, "the history charts need more than one block"
    assert {summary["chain_id"] for summary in summaries} == {1}
    assert len({summary["pair"] for summary in summaries}) == 1
    assert len({summary["samples_per_side"] for summary in summaries}) == 1
    numbers = [summary["block_number"] for summary in summaries]
    assert numbers == sorted(numbers)


def test_the_documented_block_range_is_the_one_on_disk() -> None:
    # Both the note beside the dataset and the README caption name the blocks
    # these artifacts were measured over. A recollection that leaves either
    # behind turns a provenance claim into a wrong one.
    numbers = [summary["block_number"] for summary in dataset_summaries()]
    note = (EXAMPLES / "data" / "README.md").read_text(encoding="utf-8")
    readme = (EXAMPLES.parent / "README.md").read_text(encoding="utf-8")
    for document in (note, readme):
        assert f"{min(numbers):,}" in document
        assert f"{max(numbers):,}" in document
