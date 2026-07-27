"""HTTP client for a local Fynd instance.

Fynd exposes three endpoints — `GET /v1/health`, `GET /v1/info`, `POST
/v1/quote` — and a local server authenticates nothing inbound (the Tycho key it
needs is for Fynd→Tycho, not caller→Fynd). This client is deliberately thin:
build an `Order`, get a `Quote`, done.

`quote()` defaults encoding ON (slippage "0.001", transfer_from) to match the mode
the hosted marketprice.xyz collector runs, so measured `amount_out` matches the
site. Pass `encoding=False` for pure pricing with no calldata.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from types import TracebackType
from typing import Self

import httpx

from price_of_ethereum.fynd.models import (
    EncodingOptions,
    ErrorResponse,
    HealthStatus,
    InstanceInfo,
    Order,
    Quote,
    QuoteOptions,
    QuoteRequest,
)

# Placeholder sender for quote-only requests. Fynd requires a sender on every
# order but does not check its balance when merely quoting, so a constant
# non-zero address works for both pure-pricing and encoding-on quotes.
DUMMY_SENDER = "0x0000000000000000000000000000000000000001"

# Slippage tolerance as a decimal string: "0.001" = 0.1%. Fynd requires a string
# here, not a JSON number — see the note on `EncodingOptions.slippage`.
DEFAULT_SLIPPAGE = "0.001"


class FyndError(Exception):
    """A Fynd request failed. Carries the HTTP status and the parsed
    `ErrorResponse` body when Fynd returned one (400/422/503)."""

    def __init__(self, status_code: int, body: ErrorResponse | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class FyndClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        *,
        timeout: float = 30.0,
        sender: str = DUMMY_SENDER,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.sender = sender
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

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

    def health(self, *, timeout: float | None = None) -> HealthStatus:
        # 200 = healthy, 503 = stale data; both carry a HealthStatus body.
        if timeout is None:
            response = self._http.get("/v1/health")
        else:
            response = self._http.get("/v1/health", timeout=timeout)
        if response.status_code not in (200, 503):
            response.raise_for_status()
        return HealthStatus.model_validate_json(response.content)

    def info(self) -> InstanceInfo:
        response = self._http.get("/v1/info")
        response.raise_for_status()
        return InstanceInfo.model_validate_json(response.content)

    def build_order(
        self,
        token_in: str,
        token_out: str,
        amount: int | str,
        *,
        sender: str | None = None,
        receiver: str | None = None,
    ) -> Order:
        return Order(
            token_in=token_in,
            token_out=token_out,
            amount=str(amount),
            sender=sender or self.sender,
            receiver=receiver,
        )

    def quote(
        self,
        orders: Order | Sequence[Order],
        *,
        min_responses: int | None = None,
        timeout_ms: int | None = None,
        max_gas: str | None = None,
        encoding: bool = True,
        slippage: str = DEFAULT_SLIPPAGE,
    ) -> Quote:
        """Solve one or more orders in a single request.

        A 200 response can still carry per-order failure: each `OrderQuote.status`
        may be `no_route_found`/`insufficient_liquidity`/`timeout`/`not_ready`/
        `price_check_failed`, in which case `route`/`transaction`/`fee_breakdown`
        are `None`. Only a request-level failure (400/422/503) raises `FyndError`.
        Callers must check `status` before using `route`.
        """
        order_list = [orders] if isinstance(orders, Order) else list(orders)
        encoding_options = (
            EncodingOptions(slippage=slippage, transfer_type="transfer_from") if encoding else None
        )
        options = QuoteOptions(
            timeout_ms=timeout_ms,
            min_responses=min_responses,
            max_gas=max_gas,
            encoding_options=encoding_options,
        )
        request = QuoteRequest(orders=order_list, options=options)
        response = self._http.post(
            "/v1/quote", json=request.model_dump(mode="json", exclude_none=True)
        )
        if response.status_code == 200:
            return Quote.model_validate_json(response.content)
        raise self._quote_error(response)

    def wait_until_ready(
        self,
        *,
        timeout_s: float = 300.0,
        poll_interval_s: float = 2.0,
        poll_timeout_s: float = 10.0,
    ) -> HealthStatus:
        """Poll `/v1/health` until Fynd reports healthy. Fynd cold-start
        hydration can take minutes, hence the generous default timeout.

        Each poll uses its own short `poll_timeout_s` so a single stalled request
        cannot overshoot `timeout_s` by the client's full default timeout.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                status = self.health(timeout=poll_timeout_s)
                if status.healthy:
                    return status
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Fynd not healthy within {timeout_s:.0f}s")
            time.sleep(poll_interval_s)

    @staticmethod
    def _quote_error(response: httpx.Response) -> FyndError:
        body: ErrorResponse | None = None
        try:
            body = ErrorResponse.model_validate_json(response.content)
            message = f"Fynd quote failed [{response.status_code} {body.code}]: {body.error}"
        except ValueError:
            message = f"Fynd quote failed [{response.status_code}]: {response.text[:200]}"
        return FyndError(response.status_code, body, message)
