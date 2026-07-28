"""Pydantic models for the Tycho Indexer RPC endpoints the SDK uses, transcribed
from the vendored spec (`specs/tycho.openapi.json`, v0.333.1).

Only `/v1/tokens` and `/v1/health` are modeled — the SDK uses Tycho solely to
resolve token metadata (decimals/symbol/quality/tax) for arbitrary pairs.

`Chain` stays a closed `Literal` where the SDK *chooses* the value (the client's
chain binding, `cli.CHAIN_TYCHO_HOSTS`), so those stay type-checked. Values the
SDK *receives* — a token's chain, the health state — are plain `str` with the
known members kept in module-level constants, so a Tycho release that adds a
chain or a health state parses normally instead of raising.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

Chain = Literal[
    "ethereum",
    "starknet",
    "zksync",
    "arbitrum",
    "base",
    "bsc",
    "unichain",
    "polygon",
]

HEALTH_STATUS_READY = "Ready"
KNOWN_HEALTH_STATUSES = frozenset({HEALTH_STATUS_READY, "Starting", "NotReady"})


class PaginationParams(BaseModel):
    page: int = 0
    page_size: int = 100


class PaginationResponse(BaseModel):
    page: int
    page_size: int
    total: int


class TokensRequestBody(BaseModel):
    chain: Chain | None = None
    min_quality: int | None = None
    traded_n_days_ago: int | None = None
    token_addresses: list[str] | None = None
    pagination: PaginationParams | None = None


class ResponseToken(BaseModel):
    chain: str
    address: str
    symbol: str
    decimals: int
    tax: int
    gas: list[int | None]
    quality: int

    @field_validator("chain", mode="before")
    @classmethod
    def _flatten_custom_chain(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        # The spec's `Chain` has a `{"custom": "<name>"}` variant alongside the
        # named strings; flatten it so a token on a custom chain still resolves.
        if isinstance(value, dict) and "custom" in value:
            return value["custom"]
        return value


class TokensRequestResponse(BaseModel):
    tokens: list[ResponseToken]
    pagination: PaginationResponse


class Health(BaseModel):
    status: str
    message: str | None = None

    @property
    def ready(self) -> bool:
        """True only for the exact ready state. Any state this SDK predates is
        reported as not-ready, so callers keep polling rather than proceed."""
        return self.status == HEALTH_STATUS_READY
