"""Pydantic models for the Tycho Indexer RPC endpoints the SDK uses, transcribed
from the vendored spec (`specs/tycho.openapi.json`, v0.333.1).

Only `/v1/tokens` and `/v1/health` are modeled — the SDK uses Tycho solely to
resolve token metadata (decimals/symbol/quality/tax) for arbitrary pairs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

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
    chain: Chain
    address: str
    symbol: str
    decimals: int
    tax: int
    gas: list[int | None]
    quality: int


class TokensRequestResponse(BaseModel):
    tokens: list[ResponseToken]
    pagination: PaginationResponse


class Health(BaseModel):
    status: Literal["Ready", "Starting", "NotReady"]
    message: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "Ready"
