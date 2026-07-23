"""Guardrails on the vendored OpenAPI specs.

The offline checks pin the spec versions the client models are written against.
The live check fetches the upstream specs and fails when they drift, so a Fynd
or Tycho API bump surfaces in CI instead of at runtime. The live check skips
cleanly when there is no network (local dev, offline CI).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

SPECS = Path(__file__).resolve().parent.parent / "specs"

PINS = {
    "fynd.openapi.json": {
        "version": "0.97.0",
        "url": "https://fynd-api.propellerheads.xyz/api-docs/openapi.json",
    },
    "tycho.openapi.json": {
        "version": "0.333.1",
        "url": "https://tycho-beta.propellerheads.xyz/api-docs/openapi.json",
    },
}


@pytest.mark.parametrize("filename", list(PINS))
def test_vendored_spec_version_pinned(filename: str) -> None:
    spec = json.loads((SPECS / filename).read_text())
    assert spec["info"]["version"] == PINS[filename]["version"]


@pytest.mark.parametrize("filename", list(PINS))
def test_vendored_spec_matches_upstream(filename: str) -> None:
    url = PINS[filename]["url"]
    try:
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"upstream spec unreachable ({exc!r})")

    upstream_version = response.json()["info"]["version"]
    vendored_version = json.loads((SPECS / filename).read_text())["info"]["version"]
    assert upstream_version == vendored_version, (
        f"{filename} drifted: vendored {vendored_version}, upstream {upstream_version}. "
        f"Re-vendor from {url} and update the client models."
    )
