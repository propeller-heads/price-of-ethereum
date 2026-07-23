"""Token-resolution tests over a mocked Tycho client."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from price_of_ethereum.tokens import resolve_tokens
from price_of_ethereum.tycho import TychoClient

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


def token_row(
    address: str, symbol: str, decimals: int, *, quality: int = 100, tax: int = 0
) -> dict:
    return {
        "chain": "ethereum",
        "address": address,
        "symbol": symbol,
        "decimals": decimals,
        "tax": tax,
        "gas": [50000],
        "quality": quality,
    }


def client_returning(tokens: list[dict]) -> TychoClient:
    page = {"tokens": tokens, "pagination": {"page": 0, "page_size": 100, "total": len(tokens)}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    return TychoClient("https://tycho.test", "key", transport=httpx.MockTransport(handler))


def test_resolve_keys_lowercase_and_maps_fields() -> None:
    client = client_returning([token_row(USDC, "USDC", 6), token_row(WETH, "WETH", 18)])
    resolved = resolve_tokens(client, [USDC, WETH])
    assert set(resolved) == {USDC.lower(), WETH.lower()}
    weth = resolved[WETH.lower()]
    assert weth.symbol == "WETH"
    assert weth.decimals == 18
    assert weth.is_standard


def test_missing_token_raises() -> None:
    client = client_returning([token_row(USDC, "USDC", 6)])
    with pytest.raises(LookupError):
        resolve_tokens(client, [USDC, WETH])


def test_resolve_matches_across_address_casing() -> None:
    # Request lowercase, Tycho responds checksummed — keying stays consistent.
    client = client_returning([token_row(WETH, "WETH", 18)])
    resolved = resolve_tokens(client, [WETH.lower()])
    assert resolved[WETH.lower()].symbol == "WETH"


def test_non_standard_token_warns(caplog: pytest.LogCaptureFixture) -> None:
    fee_on_transfer = token_row(WETH, "FOO", 18, quality=50, tax=200)
    client = client_returning([fee_on_transfer])
    with caplog.at_level(logging.WARNING):
        resolved = resolve_tokens(client, [WETH])
    assert not resolved[WETH.lower()].is_standard
    assert "non-standard" in caplog.text
    assert "tax=200bps" in caplog.text


def test_min_quality_forwarded() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        page = {
            "tokens": [token_row(USDC, "USDC", 6)],
            "pagination": {"page": 0, "page_size": 100, "total": 1},
        }
        return httpx.Response(200, json=page)

    client = TychoClient("https://tycho.test", "key", transport=httpx.MockTransport(handler))
    resolve_tokens(client, [USDC], min_quality=51)
    assert seen[0]["min_quality"] == 51
