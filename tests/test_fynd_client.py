"""Fynd client tests. All HTTP is served by httpx.MockTransport — no network.

Response bodies mirror the shapes in specs/fynd.openapi.json.
"""

from __future__ import annotations

import json

import httpx
import pytest

from price_of_ethereum.fynd import DUMMY_SENDER, FyndClient, FyndError, Order

HEALTHY = {
    "healthy": True,
    "last_update_ms": 1250,
    "num_solver_pools": 2,
    "derived_data_ready": True,
}
STALE = {"healthy": False, "last_update_ms": 999999, "num_solver_pools": 2}
INFO = {
    "chain_id": 1,
    "permit2_address": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
    "router_address": "0xfD0b31d2E955fA55e3fa641Fe90e08b677188d35",
    "version": "0.97.0",
}
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

QUOTE_OK = {
    "orders": [
        {
            "order_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "status": "success",
            "amount_in": "1000000000000000000",
            "amount_out": "3500000000",
            "amount_out_net_gas": "3498000000",
            "gas_estimate": "150000",
            "gas_price": "20000000000",
            "price_impact_bps": 5,
            "block": {"number": 21000000, "hash": "0xabc", "timestamp": 1730000000},
            "route": {
                "swaps": [
                    {
                        "component_id": "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc",
                        "protocol": "uniswap_v3",
                        "token_in": WETH,
                        "token_out": USDC,
                        "amount_in": "1000000000000000000",
                        "amount_out": "3500000000",
                        "gas_estimate": "150000",
                        "split": 1.0,
                    }
                ]
            },
        }
    ],
    "total_gas_estimate": "150000",
    "solve_time_ms": 12,
}
NO_ROUTE = {"error": "no route found for order", "code": "NO_ROUTE_FOUND"}


class Recorder:
    """MockTransport handler that records requests and returns scripted responses."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.routes[(request.method, request.url.path)]
        return httpx.Response(status, json=body)

    @property
    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)


def client_with(routes: dict[tuple[str, str], tuple[int, dict]]) -> tuple[FyndClient, Recorder]:
    recorder = Recorder(routes)
    return FyndClient(transport=httpx.MockTransport(recorder)), recorder


def test_health_healthy() -> None:
    client, _ = client_with({("GET", "/v1/health"): (200, HEALTHY)})
    status = client.health()
    assert status.healthy
    assert status.num_solver_pools == 2


def test_health_stale_503_still_parses() -> None:
    client, _ = client_with({("GET", "/v1/health"): (503, STALE)})
    status = client.health()
    assert status.healthy is False
    assert status.gas_price_age_ms is None


def test_info() -> None:
    client, _ = client_with({("GET", "/v1/info"): (200, INFO)})
    info = client.info()
    assert info.chain_id == 1
    assert info.router_address is not None


def test_quote_success_parses_route() -> None:
    client, _ = client_with({("POST", "/v1/quote"): (200, QUOTE_OK)})
    order = client.build_order(WETH, USDC, 10**18)
    quote = client.quote(order)
    (order_quote,) = quote.orders
    assert order_quote.status == "success"
    assert order_quote.gas_price == "20000000000"
    assert order_quote.route is not None
    assert order_quote.route.swaps[0].protocol == "uniswap_v3"


def test_quote_encoding_on_by_default() -> None:
    client, rec = client_with({("POST", "/v1/quote"): (200, QUOTE_OK)})
    client.quote(client.build_order(WETH, USDC, 10**18), min_responses=1, timeout_ms=2000)
    opts = rec.last_json["options"]
    assert opts["encoding_options"] == {"slippage": 0.001, "transfer_type": "transfer_from"}
    assert opts["min_responses"] == 1
    assert opts["timeout_ms"] == 2000


def test_quote_encoding_off_omits_encoding_options() -> None:
    client, rec = client_with({("POST", "/v1/quote"): (200, QUOTE_OK)})
    client.quote(client.build_order(WETH, USDC, 10**18), encoding=False)
    assert "encoding_options" not in rec.last_json["options"]


def test_request_payload_shape() -> None:
    client, rec = client_with({("POST", "/v1/quote"): (200, QUOTE_OK)})
    client.quote(client.build_order(WETH, USDC, 10**18))
    (sent_order,) = rec.last_json["orders"]
    assert sent_order["side"] == "sell"
    assert sent_order["sender"] == DUMMY_SENDER
    assert sent_order["amount"] == "1000000000000000000"
    assert "receiver" not in sent_order  # exclude_none


def test_quote_no_route_raises_fynderror() -> None:
    client, _ = client_with({("POST", "/v1/quote"): (422, NO_ROUTE)})
    with pytest.raises(FyndError) as excinfo:
        client.quote(client.build_order(WETH, USDC, 10**18))
    err = excinfo.value
    assert err.status_code == 422
    assert err.body is not None
    assert err.body.code == "NO_ROUTE_FOUND"


def test_build_order_defaults() -> None:
    client, _ = client_with({("GET", "/v1/info"): (200, INFO)})
    order = client.build_order(WETH, USDC, 123)
    assert isinstance(order, Order)
    assert order.sender == DUMMY_SENDER
    assert order.amount == "123"
    assert order.receiver is None


def test_wait_until_ready_returns_when_healthy() -> None:
    client, _ = client_with({("GET", "/v1/health"): (200, HEALTHY)})
    status = client.wait_until_ready(timeout_s=1.0, poll_interval_s=0.01)
    assert status.healthy


NO_ROUTE_ORDER = {
    "orders": [
        {
            "order_id": "x",
            "status": "no_route_found",
            "amount_in": "1000000000000000000",
            "amount_out": "0",
            "amount_out_net_gas": "0",
            "gas_estimate": "0",
            "block": {"number": 21000000, "hash": "0xabc", "timestamp": 1730000000},
        }
    ],
    "total_gas_estimate": "0",
    "solve_time_ms": 5,
}


def test_health_server_error_raises() -> None:
    client, _ = client_with({("GET", "/v1/health"): (500, {"error": "boom"})})
    with pytest.raises(httpx.HTTPStatusError):
        client.health()


def test_info_empty_version_is_none() -> None:
    client, _ = client_with({("GET", "/v1/info"): (200, {**INFO, "version": ""})})
    assert client.info().version is None


def test_quote_multiple_orders_sends_all() -> None:
    client, rec = client_with({("POST", "/v1/quote"): (200, QUOTE_OK)})
    orders = [client.build_order(WETH, USDC, 10**18), client.build_order(USDC, WETH, 3_500_000_000)]
    client.quote(orders)
    assert len(rec.last_json["orders"]) == 2


def test_quote_non_success_status_parses_with_null_fields() -> None:
    client, _ = client_with({("POST", "/v1/quote"): (200, NO_ROUTE_ORDER)})
    quote = client.quote(client.build_order(WETH, USDC, 10**18))
    (order_quote,) = quote.orders
    assert order_quote.status == "no_route_found"
    assert order_quote.route is None
    assert order_quote.gas_price is None
    assert order_quote.price_impact_bps is None


@pytest.mark.parametrize("status_code", [400, 503])
def test_quote_request_level_errors_raise(status_code: int) -> None:
    client, _ = client_with(
        {("POST", "/v1/quote"): (status_code, {"error": "nope", "code": "BAD_REQUEST"})}
    )
    with pytest.raises(FyndError) as excinfo:
        client.quote(client.build_order(WETH, USDC, 10**18))
    assert excinfo.value.status_code == status_code
    assert excinfo.value.body is not None


def test_context_manager_closes_underlying_client() -> None:
    with FyndClient(
        transport=httpx.MockTransport(Recorder({("GET", "/v1/info"): (200, INFO)}))
    ) as (client):
        assert client.info().chain_id == 1
    assert client._http.is_closed


def test_build_order_explicit_sender_receiver() -> None:
    client, _ = client_with({("GET", "/v1/info"): (200, INFO)})
    order = client.build_order(WETH, USDC, 1, sender="0xAAA", receiver="0xBBB")
    assert order.sender == "0xAAA"
    assert order.receiver == "0xBBB"


def test_wait_until_ready_times_out() -> None:
    client, _ = client_with({("GET", "/v1/health"): (503, STALE)})
    with pytest.raises(TimeoutError):
        client.wait_until_ready(timeout_s=0.05, poll_interval_s=0.01, poll_timeout_s=0.05)


def test_wait_until_ready_retries_after_transport_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=HEALTHY)

    client = FyndClient(transport=httpx.MockTransport(handler))
    status = client.wait_until_ready(timeout_s=1.0, poll_interval_s=0.01, poll_timeout_s=0.5)
    assert status.healthy
    assert calls["n"] == 2
