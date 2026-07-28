"""Tycho client tests over httpx.MockTransport — no network."""

from __future__ import annotations

import json

import httpx
import pytest

from price_of_ethereum.tycho import TychoClient, TychoError
from price_of_ethereum.tycho.models import HEALTH_STATUS_READY, KNOWN_HEALTH_STATUSES

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


class TychoStub:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "Ready"})
        body = json.loads(request.content)
        page = body.get("pagination", {}).get("page", 0)
        return httpx.Response(200, json=self.pages[page])


def client_with(pages: list[dict], *, api_key: str = "secret") -> tuple[TychoClient, TychoStub]:
    stub = TychoStub(pages)
    client = TychoClient("https://tycho.test", api_key, transport=httpx.MockTransport(stub))
    return client, stub


def health_client(body: dict) -> TychoClient:
    return TychoClient(
        "https://tycho.test",
        "k",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )


def test_health_ready() -> None:
    client, _ = client_with([])
    health = client.health()
    assert health.ready
    assert health.status == HEALTH_STATUS_READY


def test_health_state_the_sdk_has_never_seen_parses_as_not_ready() -> None:
    # A Tycho release that adds a health state must not turn a readiness poll
    # into a crash — an unknown state is simply not ready.
    health = health_client({"status": "Reindexing", "message": "catching up"}).health()
    assert health.status == "Reindexing"
    assert health.status not in KNOWN_HEALTH_STATUSES
    assert not health.ready
    assert health.message == "catching up"


def test_token_on_a_chain_the_sdk_has_never_seen_parses() -> None:
    page = {
        "tokens": [{**token_row(USDC, "USDC", 6), "chain": "monad"}],
        "pagination": {"page": 0, "page_size": 100, "total": 1},
    }
    client, _ = client_with([page])
    (token,) = client.tokens(addresses=[USDC])
    assert token.chain == "monad"
    assert token.decimals == 6


def test_token_on_a_custom_chain_is_flattened() -> None:
    # The spec's Chain has a {"custom": "<name>"} variant next to the named ones.
    page = {
        "tokens": [{**token_row(USDC, "USDC", 6), "chain": {"custom": "devnet"}}],
        "pagination": {"page": 0, "page_size": 100, "total": 1},
    }
    client, _ = client_with([page])
    (token,) = client.tokens(addresses=[USDC])
    assert token.chain == "devnet"


def test_tokens_single_page() -> None:
    page = {
        "tokens": [token_row(USDC, "USDC", 6), token_row(WETH, "WETH", 18)],
        "pagination": {"page": 0, "page_size": 100, "total": 2},
    }
    client, _ = client_with([page])
    tokens = client.tokens(addresses=[USDC, WETH])
    assert [t.symbol for t in tokens] == ["USDC", "WETH"]
    assert tokens[0].decimals == 6


def test_tokens_follows_pagination() -> None:
    pages = [
        {
            "tokens": [token_row(USDC, "USDC", 6)],
            "pagination": {"page": 0, "page_size": 1, "total": 2},
        },
        {
            "tokens": [token_row(WETH, "WETH", 18)],
            "pagination": {"page": 1, "page_size": 1, "total": 2},
        },
    ]
    client, stub = client_with(pages)
    tokens = client.tokens()
    assert [t.symbol for t in tokens] == ["USDC", "WETH"]
    assert len(stub.requests) == 2


def test_auth_header_sent() -> None:
    page = {"tokens": [], "pagination": {"page": 0, "page_size": 100, "total": 0}}
    client, stub = client_with([page], api_key="my-key")
    client.tokens(addresses=[USDC])
    assert stub.requests[-1].headers["authorization"] == "my-key"


def test_non_200_raises_tychoerror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = TychoClient("https://tycho.test", "bad", transport=httpx.MockTransport(handler))
    with pytest.raises(TychoError) as excinfo:
        client.tokens(addresses=[USDC])
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize(
    ("status", "message"),
    [("Starting", "warming up"), ("NotReady", "No db connection")],
)
def test_health_not_ready_variants(status: str, message: str) -> None:
    health = health_client({"status": status, "message": message}).health()
    assert not health.ready
    assert health.message == message


def test_pagination_empty_page_terminates() -> None:
    # Server reports total=99 but the next page comes back empty (transient
    # hiccup); the loop must terminate instead of spinning forever.
    pages = [
        {
            "tokens": [token_row(USDC, "USDC", 6)],
            "pagination": {"page": 0, "page_size": 1, "total": 99},
        },
        {"tokens": [], "pagination": {"page": 1, "page_size": 1, "total": 99}},
    ]
    client, stub = client_with(pages)
    tokens = client.tokens()
    assert [t.symbol for t in tokens] == ["USDC"]
    assert len(stub.requests) == 2


def test_context_manager_closes_underlying_client() -> None:
    page = {"tokens": [], "pagination": {"page": 0, "page_size": 100, "total": 0}}
    with TychoClient(
        "https://tycho.test", "k", transport=httpx.MockTransport(TychoStub([page]))
    ) as client:
        client.tokens(addresses=[USDC])
    assert client._http.is_closed


def test_tokens_chain_override_in_request() -> None:
    page = {"tokens": [], "pagination": {"page": 0, "page_size": 100, "total": 0}}
    client, stub = client_with([page])
    client.tokens(addresses=[USDC], chain="base")
    assert json.loads(stub.requests[-1].content)["chain"] == "base"


def test_traded_n_days_ago_forwarded() -> None:
    page = {"tokens": [], "pagination": {"page": 0, "page_size": 100, "total": 0}}
    client, stub = client_with([page])
    client.tokens(addresses=[USDC], traded_n_days_ago=3)
    assert json.loads(stub.requests[-1].content)["traded_n_days_ago"] == 3
