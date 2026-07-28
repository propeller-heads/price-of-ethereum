# Contributing

## Dev setup

```bash
uv sync --all-extras --dev
```

This installs the package editable, plus the `data`, `parquet` and `viz` extras
and the dev tools (`pytest`, `ruff`, `ty`).

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
