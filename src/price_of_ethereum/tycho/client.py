"""HTTP client for the Tycho Indexer RPC.

The SDK uses Tycho only to resolve token metadata via `POST /v1/tokens`. Tycho
authenticates every request with an API key in the `authorization` header (the
same key a local Fynd uses to reach Tycho). One client is bound to one chain.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Self

import httpx

from price_of_ethereum.tycho.models import (
    Chain,
    Health,
    PaginationParams,
    ResponseToken,
    TokensRequestBody,
    TokensRequestResponse,
)

# Tycho enforces a max page size of 100.
MAX_PAGE_SIZE = 100


class TychoError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class TychoClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        chain: Chain = "ethereum",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.chain = chain
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"authorization": api_key},
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def health(self) -> Health:
        response = self._http.get("/v1/health")
        if response.status_code != 200:
            raise TychoError(response.status_code, f"Tycho health failed: {response.text[:200]}")
        return Health.model_validate_json(response.content)

    def tokens(
        self,
        addresses: Sequence[str] | None = None,
        *,
        min_quality: int | None = None,
        traded_n_days_ago: int | None = None,
        chain: Chain | None = None,
    ) -> list[ResponseToken]:
        """Fetch token metadata, following pagination until every match is
        collected. Pass `addresses` to resolve a specific set."""
        wanted_chain = chain or self.chain
        # None = fetch all; an explicit (possibly empty) sequence is a real filter.
        address_filter = None if addresses is None else list(addresses)
        collected: list[ResponseToken] = []
        page = 0
        while True:
            body = TokensRequestBody(
                chain=wanted_chain,
                min_quality=min_quality,
                traded_n_days_ago=traded_n_days_ago,
                token_addresses=address_filter,
                pagination=PaginationParams(page=page, page_size=MAX_PAGE_SIZE),
            )
            response = self._http.post(
                "/v1/tokens", json=body.model_dump(mode="json", exclude_none=True)
            )
            if response.status_code != 200:
                raise TychoError(
                    response.status_code, f"Tycho tokens failed: {response.text[:200]}"
                )
            parsed = TokensRequestResponse.model_validate_json(response.content)
            collected.extend(parsed.tokens)
            page += 1
            if not parsed.tokens or len(collected) >= parsed.pagination.total:
                return collected
