"""A fabricated Fynd, so the examples run with no server and no API key.

Nothing this produces is a measurement. It is a two-pool constant-product AMM
with an invented block clock, wired into `FyndClient` through an
`httpx.MockTransport` so the SDK's real request path, sweep, bisection and
collector all run unchanged against it.

Everything it emits is named so it cannot be mistaken for mainnet: the pair is
`simETH/simUSDC`, the chain id is 31337 (the local-devnet id, not Ethereum), the
protocols are `simulated_cpmm_*` and the block numbers start at 1,000,000,000 —
a height no chain here has reached. Those names travel into every chart axis,
file name and report title downstream, which is the point.

Real numbers need a real Fynd and a Tycho key; the README's setup section is
five steps. `examples/quickstart.ipynb` uses this only when nothing is listening
on the Fynd port.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from price_of_ethereum import FyndClient, TokenMeta

SIM_TOKEN = TokenMeta(
    address="0x5151515151515151515151515151515151515151",
    symbol="simETH",
    decimals=18,
    quality=100,
    tax=0,
)
SIM_NUMERAIRE = TokenMeta(
    address="0x5555555555555555555555555555555555555555",
    symbol="simUSDC",
    decimals=6,
    quality=100,
    tax=0,
)
SIM_PAIR = "simETH/simUSDC"
SIM_CHAIN_ID = 31337
SIM_SEARCH_MIN = 50.0
SIM_SEARCH_MAX = 5_000_000.0

SIM_FIRST_BLOCK = 1_000_000_000
SIM_FIRST_TIMESTAMP = 1_700_000_000
SIM_BLOCK_SECONDS = 12

GAS_PER_POOL = 150_000
BASE_GAS_PRICE_WEI = 2 * 10**9

# Splits the "router" is allowed to consider, as eighths of the input. A real
# solver searches continuously; this grid is what makes measured impact stop
# being perfectly monotonic here, the same way route recomposition does against
# real liquidity.
SPLIT_STEPS = 8


@dataclass(frozen=True)
class Pool:
    component_id: str
    protocol: str
    numeraire_reserve: int
    token_reserve: int
    fee_bps: int


# Both pools sit at 2500 simUSDC per simETH, so neither is a standing arbitrage:
# one deep and cheap, one shallow and expensive, which is what gives the cost
# curve a shape worth plotting.
POOLS = (
    Pool(
        component_id="0xaaaa111111111111111111111111111111111111",
        protocol="simulated_cpmm_deep",
        numeraire_reserve=250_000_000 * 10**6,
        token_reserve=100_000 * 10**18,
        fee_bps=5,
    ),
    Pool(
        component_id="0xbbbb222222222222222222222222222222222222",
        protocol="simulated_cpmm_shallow",
        numeraire_reserve=30_000_000 * 10**6,
        token_reserve=12_000 * 10**18,
        fee_bps=30,
    ),
)


def price_drift(block_index: int) -> float:
    """Multiplier on every numeraire reserve at `block_index`.

    Two incommensurate sines rather than a random walk, so the recorded history
    is the same shape every time this is regenerated.
    """
    return 1.0 + 0.006 * math.sin(block_index / 2.9) + 0.0025 * math.sin(block_index / 1.3 + 1.0)


def swap_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    charged = amount_in * (10_000 - fee_bps) // 10_000
    return reserve_out * charged // (reserve_in + charged)


def best_split(amount_in: int, reserves: list[tuple[int, int, int]]) -> tuple[list[int], list[int]]:
    """Per-pool (inputs, outputs) for the split that maximises total output."""
    best_inputs = [amount_in, 0]
    best_outputs = [0, 0]
    for step in range(SPLIT_STEPS + 1):
        first = amount_in * step // SPLIT_STEPS
        inputs = [first, amount_in - first]
        outputs = [
            swap_out(amount, reserve_in, reserve_out, fee_bps) if amount else 0
            for amount, (reserve_in, reserve_out, fee_bps) in zip(inputs, reserves, strict=True)
        ]
        if sum(outputs) > sum(best_outputs):
            best_inputs, best_outputs = inputs, outputs
    return best_inputs, best_outputs


class SimulatedChain:
    """The fabricated server, including its clock.

    The block advances on the first request to arrive after a quiet gap. A sweep
    fires its quotes back to back, so every snapshot sees exactly one block; the
    collector's idle probe waits `poll_interval_s` between cycles, so that probe is
    what moves the chain on. The result is consecutive blocks with no straddles,
    without the simulator needing to recognise which quote is which.
    """

    QUIET_GAP_SECONDS = 1.0

    def __init__(self) -> None:
        self._block_index = 0
        self._last_request_at = time.monotonic()
        self._lock = threading.Lock()

    def _tick(self) -> int:
        with self._lock:
            now = time.monotonic()
            if now - self._last_request_at >= self.QUIET_GAP_SECONDS:
                self._block_index += 1
            self._last_request_at = now
            return self._block_index

    def handle(self, request: httpx.Request) -> httpx.Response:
        block_index = self._tick()
        if request.url.path == "/v1/health":
            return httpx.Response(
                200,
                json={
                    "healthy": True,
                    "last_update_ms": 0,
                    "num_solver_pools": len(POOLS),
                    "derived_data_ready": True,
                },
            )
        if request.url.path == "/v1/info":
            return httpx.Response(
                200,
                json={
                    "chain_id": SIM_CHAIN_ID,
                    "permit2_address": "0x0000000000000000000000000000000000000000",
                    "router_address": "0x0000000000000000000000000000000000000000",
                    "version": "simulated",
                },
            )
        if request.url.path == "/v1/quote":
            orders = json.loads(request.content)["orders"]
            return httpx.Response(
                200,
                json={
                    "orders": [
                        self._quote(order, block_index, index) for index, order in enumerate(orders)
                    ],
                    "total_gas_estimate": str(GAS_PER_POOL * len(POOLS)),
                    "solve_time_ms": 4 + block_index % 5,
                },
            )
        return httpx.Response(
            404, json={"error": f"simulated Fynd has no {request.url.path}", "code": "not_found"}
        )

    def _quote(self, order: dict[str, Any], block_index: int, index: int) -> dict[str, Any]:
        buying_token = order["token_in"].lower() == SIM_NUMERAIRE.address.lower()
        drift = price_drift(block_index)
        pools = [
            (
                int(pool.numeraire_reserve * drift) if buying_token else pool.token_reserve,
                pool.token_reserve if buying_token else int(pool.numeraire_reserve * drift),
                pool.fee_bps,
            )
            for pool in POOLS
        ]
        amount_in = int(order["amount"])
        inputs, outputs = best_split(amount_in, pools)
        gas_estimate = GAS_PER_POOL * sum(1 for amount in inputs if amount)
        gas_price_wei = int(BASE_GAS_PRICE_WEI * (1.0 + 0.2 * math.sin(block_index / 1.7)))
        amount_out = sum(outputs)
        return {
            "order_id": f"sim-{SIM_FIRST_BLOCK + block_index}-{index}",
            "status": "success",
            "amount_in": str(amount_in),
            "amount_out": str(amount_out),
            "amount_out_net_gas": str(
                max(amount_out - self._gas_cost(gas_estimate, gas_price_wei, buying_token), 0)
            ),
            "gas_estimate": str(gas_estimate),
            "gas_price": str(gas_price_wei),
            "price_impact_bps": None,
            "block": {
                "number": SIM_FIRST_BLOCK + block_index,
                # A hash that is visibly the block number, so nobody greps for it
                # expecting to find a chain that carries it.
                "hash": f"0x{SIM_FIRST_BLOCK + block_index:064x}",
                "timestamp": SIM_FIRST_TIMESTAMP + SIM_BLOCK_SECONDS * block_index,
            },
            "route": {
                "swaps": [
                    {
                        "component_id": pool.component_id,
                        "protocol": pool.protocol,
                        "token_in": order["token_in"],
                        "token_out": order["token_out"],
                        "amount_in": str(pool_in),
                        "amount_out": str(pool_out),
                        "gas_estimate": str(GAS_PER_POOL),
                        "split": pool_in / amount_in if amount_in else 0.0,
                    }
                    for pool, pool_in, pool_out in zip(POOLS, inputs, outputs, strict=True)
                    if pool_in
                ]
            },
            # A simulator cannot encode calldata anyone could send, so it does
            # not pretend to: anchored levels record `no_transaction`.
            "transaction": None,
            "fee_breakdown": None,
        }

    def _gas_cost(self, gas_estimate: int, gas_price_wei: int, buying_token: bool) -> int:
        """Gas cost denominated in the output token's base units."""
        cost_wei = gas_estimate * gas_price_wei
        if buying_token:
            return cost_wei  # simETH is 18 decimals, like the gas token itself.
        price = (POOLS[0].numeraire_reserve / 10**6) / (POOLS[0].token_reserve / 10**18)
        return int(cost_wei / 10**18 * price * 10**6)


def simulated_fynd() -> FyndClient:
    """A `FyndClient` that answers from the simulator instead of the network."""
    chain = SimulatedChain()
    return FyndClient("http://simulated-fynd.invalid", transport=httpx.MockTransport(chain.handle))
