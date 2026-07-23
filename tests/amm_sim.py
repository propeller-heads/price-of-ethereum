"""Deterministic constant-product AMM that fabricates Fynd order quotes.

Shared by the golden-parity fixture generator (which runs the reference
marketprice.xyz collector against this simulator over HTTP) and the repo tests
(which serve it through an httpx.MockTransport) — both sides must see the exact
same integer math for the golden numbers to be comparable.
"""

from __future__ import annotations

USDC_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

# 250M USDC vs 100k WETH: marginal price 2500 numeraire per token.
NUMERAIRE_RESERVE = 250_000_000 * 10**6
TOKEN_RESERVE = 100_000 * 10**18

GAS_ESTIMATE = 150_000
GAS_PRICE_WEI = 2 * 10**9
# Flat atomic discount so amount_out_net_gas < amount_out deterministically.
NET_GAS_DISCOUNT = 1_000

BLOCK_NUMBER = 12_345
BLOCK_HASH = "0x" + "ab" * 32
BLOCK_TIMESTAMP = 1_700_000_000

POOL_COMPONENT_ID = "0x" + "01" * 20
POOL_PROTOCOL = "uniswap_v3"

SOLVE_TIME_MS = 7


def swap_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    return reserve_out * amount_in // (reserve_in + amount_in)


def order_quote(token_in: str, amount: str) -> dict:
    """One successful Fynd OrderQuote dict for a swap against the pool."""
    amount_in = int(amount)
    if token_in.lower() == USDC_ADDRESS.lower():
        amount_out = swap_out(amount_in, NUMERAIRE_RESERVE, TOKEN_RESERVE)
        token_out = WETH_ADDRESS
    else:
        amount_out = swap_out(amount_in, TOKEN_RESERVE, NUMERAIRE_RESERVE)
        token_out = USDC_ADDRESS
    return {
        "order_id": "order-1",
        "status": "success",
        "amount_in": str(amount_in),
        "amount_out": str(amount_out),
        "amount_out_net_gas": str(max(amount_out - NET_GAS_DISCOUNT, 0)),
        "gas_estimate": str(GAS_ESTIMATE),
        "gas_price": str(GAS_PRICE_WEI),
        "price_impact_bps": None,
        "block": {
            "number": BLOCK_NUMBER,
            "hash": BLOCK_HASH,
            "timestamp": BLOCK_TIMESTAMP,
        },
        "route": {
            "swaps": [
                {
                    "component_id": POOL_COMPONENT_ID,
                    "protocol": POOL_PROTOCOL,
                    "token_in": token_in,
                    "token_out": token_out,
                    "amount_in": str(amount_in),
                    "amount_out": str(amount_out),
                    "gas_estimate": str(GAS_ESTIMATE),
                    "split": 0.0,
                }
            ]
        },
        "transaction": None,
        "fee_breakdown": None,
    }


def quote_response(token_in: str, amount: str) -> dict:
    return {
        "orders": [order_quote(token_in, amount)],
        "total_gas_estimate": str(GAS_ESTIMATE),
        "solve_time_ms": SOLVE_TIME_MS,
    }
