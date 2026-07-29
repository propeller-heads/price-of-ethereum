# Contributing

## Dev setup

```bash
uv sync --all-extras --dev
```

This installs the package editable, plus the `data`, `parquet` and `viz` extras
and the dev tools (`pytest`, `ruff`, `ty`).

The uv version is pinned in `pyproject.toml` under `[tool.uv] required-version`,
because CI syncs with `--locked` and the lockfile has to match the format the
running uv expects. Bump that field and re-lock in the same commit.

If you re-generate `uv.lock`, use `uv lock --no-config`. A personal setting in
`~/.config/uv/uv.toml` — `exclude-newer` in particular — is otherwise written
into the lockfile, and CI, which has no such file, then re-resolves and fails
`--locked` with "Resolving despite existing lockfile due to removal of global
exclude newer".

### Without uv

uv is how CI builds and how the lockfile is maintained, but nothing here needs
it. The equivalent setup, and the same four checks as plain console scripts:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[data,viz,parquet]" --group dev   # pip >= 25.1

ruff check
ruff format --check
ty check
pytest
```

Three details this route depends on. Install all three extras — `parquet` is in
`--all-extras` and the suite covers parquet conversion, so leaving it out fails
one test. Activate the virtualenv rather than running `.venv/bin/ty`, which
resolves imports against whatever environment is active and otherwise reports
every third-party import as unresolved. And `ruff` and `ty` are pinned to exact
versions in `[dependency-groups]` so pip installs what CI runs; a newer `ruff`
formats files this one leaves alone.

Regenerating `uv.lock` is the one task that genuinely requires uv.

## Checks

Run all four before opening a PR — CI runs the same commands:

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```

`uv run pytest` is hermetic: nothing in it reaches the network. The one check
that does — comparing the vendored OpenAPI specs against the live Fynd and
Tycho documents — is opt-in, and a scheduled workflow runs it weekly:

```bash
POE_LIVE_SPEC_CHECK=1 uv run pytest tests/test_specs.py
```

A failure there means an upstream service shipped a release and `specs/` is
stale, not that this package is broken. Re-vendor the changed document; the
offline contract tests in the same file say whether anything the SDK actually
depends on moved.

## The committed example artifacts

`examples/report.html`, `examples/images/*.png`, `examples/data/README.md` and
the stored outputs inside `examples/quickstart.ipynb` are generated, and one
command rebuilds all of them from the dataset in `examples/data`:

```bash
uv run --with nbconvert --with ipykernel --with kaleido \
    python examples/build_showcase.py
```

Only the notebook step needs a running Fynd — it executes every cell, so its
stored outputs are a live measurement. Everything else reads the committed
dataset, which is why the report stays reproducible by anyone.

Those three tools are named on that command line rather than declared as a
dependency group. `nbconvert` and `ipykernel` execute the notebook and `kaleido`
rasterises the figures, none of which this package installs, imports or tests
against; declaring them would put a compiled JSON extension and a C parser in
the lockfile permanently to serve a command that runs when the artifacts change.
kaleido from v1 drives a headless Chrome that it does not bundle, using an
installed one or a private copy fetched by `plotly_get_chrome`. The screenshot
step needs that same Chrome and is skipped with a message when there is none.

Three things about it are deliberate. Every Plotly output in the notebook gets a
PNG injected beside it, because GitHub's notebook viewer runs no JavaScript and
would otherwise show a blank gap. The notebook must be executed against a real
Fynd, since with none listening it silently falls back to
`examples/simulated_fynd.py` and would commit fabricated numbers under a header
promising measured ones. And nothing else may collect into `examples/data`:
`poe collect` appends, so a second run would splice a different pair or sweep
width into one history. `tests/test_examples.py` fails on all three.

Recollecting is `poe collect --blocks 12 --out examples/data` into an emptied
directory. The dataset is ~2.0 MB, the one place `.gitignore` lets a measured
dataset be committed, and it exists so the report can be rebuilt without a
server. Rebuilding `report.html` itself costs ~1.7 MB of git history each time,
since Plotly's bundle is inlined in it — do that when the report changes, not to
refresh the sample.

## Commit messages

This repo follows [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):
`<type>(<scope>): <description>`, where `<type>` is one of `feat`, `fix`,
`docs`, `refactor`, `test`, `ci`, `chore`. Breaking changes get a `!` before
the colon and a `BREAKING CHANGE:` footer.

## The measurement regression test

`tests/test_measurement_regression.py` pins our clean-room measurement method
against `tests/fixtures/expected_snapshot.json`, produced by the production
marketprice.xyz collector run against the identical AMM fixture in
`tests/amm_sim.py`. Every number in that fixture is bit-for-bit — spot,
robust_mid, median_depth, the curve, the anchored levels, derived price-impact
bps, route metadata, all of it.

Do not "fix" this test by updating the fixture to match new output. A failure
here means the measurement method diverged from production, which is exactly
what the test exists to catch. If you believe the reference fixture itself is
wrong, that's a discussion to have before touching it, not a one-line fix.

**The fixture is currently self-referential, and that is a known gap.** Price
impact used to be measured against `spot`, which is a one-directional probe that
buys the token and is therefore an ask, not a mid. That charged the whole
bid/ask spread to the sell side and let a buy show negative cost. Impact is now
measured against `robust_mid`, which is two-sided by construction, and the
fixture was regenerated for it: every buy impact rose by half the spread, every
sell impact fell by the same, and every sell `price_impact_bps` changed sign.
The arithmetic is checkable — a $50,000 buy on the simulated pool is priced at
exactly 2500.5, which is exactly 0.02% over a 2500.0 mid and an awkward 0.0196%
over the 2500.01 ask.

Until the production collector measures impact the same way, this file no longer
cross-checks anything outside this repository; it pins this SDK against itself.
Restoring the independent signal means regenerating it from production once
production carries the same reference.
