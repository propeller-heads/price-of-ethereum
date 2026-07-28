"""HTTP client for a local Fynd instance.

Fynd exposes three endpoints — `GET /v1/health`, `GET /v1/info`, `POST
/v1/quote` — and a local server authenticates nothing inbound (the Tycho key it
needs is for Fynd→Tycho, not caller→Fynd). This client is deliberately thin:
build an `Order`, get a `Quote`, done.

`quote()` defaults encoding ON (slippage "0.001", transfer_from), which returns
executable calldata and a fee breakdown alongside the quote. `amount_out` is the
same either way — it is gross of fees in both modes. Pass `encoding=False` for
pricing only.
"""

from __future__ import annotations

import logging
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

# How often to repeat the waiting-for-Fynd line, so a long cold start shows
# progress without flooding the log.
PROGRESS_INTERVAL_S = 30.0

logger = logging.getLogger(__name__)

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
        # Kept alongside the client so failures can name the URL they tried;
        # "not healthy" is unactionable without it.
        self.base_url = base_url
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

        Progress is logged rather than waited out in silence: minutes of no
        output is indistinguishable from a hung process, and the usual cause is
        that nothing is listening on the URL being polled.
        """
        deadline = time.monotonic() + timeout_s
        started = time.monotonic()
        announced = False
        while True:
            try:
                status = self.health(timeout=poll_timeout_s)
                if status.healthy:
                    return status
                reason = "hydrating"
            except httpx.HTTPError as error:
                reason = f"unreachable ({type(error).__name__})"
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Fynd at {self.base_url} not healthy within {timeout_s:.0f}s: {reason}. "
                    "Start it with `fynd serve`, or point --fynd-url at a running instance."
                )
            if not announced:
                logger.info(
                    "waiting up to %.0fs for Fynd at %s (%s)", timeout_s, self.base_url, reason
                )
                announced = True
            elif time.monotonic() - started > PROGRESS_INTERVAL_S:
                logger.info(
                    "still waiting for Fynd at %s after %.0fs (%s)",
                    self.base_url,
                    time.monotonic() - started,
                    reason,
                )
                started = time.monotonic()
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
