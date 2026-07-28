"""Pydantic models for the Fynd `/v1` API, transcribed from the vendored
OpenAPI spec (`specs/fynd.openapi.json`, v0.97.3).

Request models serialize with `exclude_none=True` so optional fields are omitted
rather than sent as `null`. Amounts are decimal strings in base (atomic) units
throughout — parse to `int`/`Decimal` in pricing code, never `float`.

Only the request fields the SDK actually sends are modeled (slippage,
transfer_type); the advanced encoding inputs (permit2, price guard, client fee)
are added when execution encoding is built. Response models are complete and
tolerate the spec's nullable fields.

Enums the SDK *sends* stay closed `Literal`s — the SDK picks those values, so a
closed set catches our own mistakes. Enums the SDK *receives* are plain `str`
with the known members kept in a module-level frozenset, so a Fynd release that
adds a status parses normally instead of voiding the entire response.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

OrderSide = Literal["sell"]
UserTransferType = Literal["transfer_from_permit2", "transfer_from", "use_vaults_funds"]

QUOTE_STATUS_SUCCESS = "success"
KNOWN_QUOTE_STATUSES = frozenset(
    {
        QUOTE_STATUS_SUCCESS,
        "no_route_found",
        "insufficient_liquidity",
        "timeout",
        "not_ready",
        "price_check_failed",
    }
)


class Order(BaseModel):
    token_in: str
    token_out: str
    amount: str
    side: OrderSide = "sell"
    sender: str
    receiver: str | None = None


class EncodingOptions(BaseModel):
    # Sent as a decimal string, not a number: the server's deserializer rejects a
    # JSON float ("invalid type: floating point `0.001`, expected a string").
    # Observed against a running Fynd binary reporting version 0.97.4 — that is
    # the server's own build version from `GET /v1/info`, not an OpenAPI spec
    # version, and it is ahead of the spec this file transcribes (0.97.3). The
    # schema types this as `number` while its own example quotes it; the example
    # is the accurate half, so the wire type is pinned here and the spec contract
    # check asserts that example stays a string.
    slippage: str = Field(description="Slippage tolerance as a decimal string: '0.001' = 0.1%.")
    transfer_type: UserTransferType | None = None


class QuoteOptions(BaseModel):
    timeout_ms: int | None = None
    min_responses: int | None = None
    max_gas: str | None = None
    encoding_options: EncodingOptions | None = None


class QuoteRequest(BaseModel):
    orders: list[Order]
    options: QuoteOptions | None = None


class BlockInfo(BaseModel):
    number: int
    hash: str
    timestamp: int


class Swap(BaseModel):
    component_id: str
    protocol: str
    token_in: str
    token_out: str
    amount_in: str
    amount_out: str
    gas_estimate: str
    split: float


class Route(BaseModel):
    swaps: list[Swap]


class FeeBreakdown(BaseModel):
    router_fee: str
    client_fee: str
    max_slippage: str
    min_amount_received: str
    swaps_hash: str | None = None


class Transaction(BaseModel):
    to: str
    value: str
    data: str
    client_fee_signature_offset: int | None = None


class OrderQuote(BaseModel):
    order_id: str
    # `str` rather than a closed enum: a status this SDK predates must not void
    # the whole quote response. Callers gate on `== QUOTE_STATUS_SUCCESS`, so an
    # unrecognised value is treated as a failed order and skipped, never as
    # success. `KNOWN_QUOTE_STATUSES` holds the ones the vendored spec declares.
    status: str
    amount_in: str
    amount_out: str
    amount_out_net_gas: str
    gas_estimate: str
    block: BlockInfo
    gas_price: str | None = None
    price_impact_bps: int | None = None
    route: Route | None = None
    transaction: Transaction | None = None
    fee_breakdown: FeeBreakdown | None = None


class Quote(BaseModel):
    orders: list[OrderQuote]
    total_gas_estimate: str
    solve_time_ms: int


class HealthStatus(BaseModel):
    healthy: bool
    last_update_ms: int
    num_solver_pools: int
    derived_data_ready: bool | None = None
    gas_price_age_ms: int | None = None


class InstanceInfo(BaseModel):
    chain_id: int
    permit2_address: str
    router_address: str | None = None
    version: str | None = None

    @field_validator("version", mode="before")
    @classmethod
    def _empty_version_is_none(cls, value: str | None) -> str | None:
        # Fynd sends "" (not omission) for servers predating the version field.
        return value or None


class ErrorResponse(BaseModel):
    error: str
    code: str
    details: Any = None
