"""Guardrails on the vendored OpenAPI specs.

Three layers, and everything in the default `pytest` run is offline:

* the version pin — the provenance record for the models transcribed by hand
  from each spec;
* the contract check — every endpoint, request field, response field and enum
  member the SDK actually depends on, asserted against the vendored spec. This
  is the real regression net: it stays green across upstream releases that
  change nothing the SDK touches, and it names the exact field when one goes
  missing;
* the live drift check — fetches both upstream specs and compares versions.
  Opt in with `POE_LIVE_SPEC_CHECK=1`, otherwise it is skipped, so a plain
  `pytest` never reaches PropellerHeads-hosted infrastructure from a fork:

      POE_LIVE_SPEC_CHECK=1 uv run pytest tests/test_specs.py

The contract tables below are transcribed from `src/price_of_ethereum/fynd/`
and `src/price_of_ethereum/tycho/`: every entry is a name that some line of
client or model code sends or reads. Upstream *adding* things is always fine —
the checks assert presence, never equality — so only a genuine removal or
rename fails them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

from price_of_ethereum.fynd.models import KNOWN_QUOTE_STATUSES, QUOTE_STATUS_SUCCESS
from price_of_ethereum.tycho.models import (
    HEALTH_STATUS_READY,
    KNOWN_HEALTH_STATUSES,
    Chain,
)

SPECS = Path(__file__).resolve().parent.parent / "specs"

LIVE_CHECK_ENV = "POE_LIVE_SPEC_CHECK"

PINS = {
    "fynd.openapi.json": {
        "version": "0.97.3",
        "url": "https://fynd-api.propellerheads.xyz/api-docs/openapi.json",
        "models": "src/price_of_ethereum/fynd/models.py",
    },
    "tycho.openapi.json": {
        "version": "0.333.1",
        "url": "https://tycho-beta.propellerheads.xyz/api-docs/openapi.json",
        "models": "src/price_of_ethereum/tycho/models.py",
    },
}

# Endpoints the clients call, and the schema each response status must still
# carry. Fynd's 503 on /v1/health matters: `FyndClient.health` parses it as a
# HealthStatus rather than raising.
ENDPOINT_RESPONSES: dict[str, dict[tuple[str, str], dict[str, str]]] = {
    "fynd.openapi.json": {
        ("get", "/v1/health"): {"200": "HealthStatus", "503": "HealthStatus"},
        ("get", "/v1/info"): {"200": "InstanceInfo"},
        ("post", "/v1/quote"): {
            "200": "Quote",
            "400": "ErrorResponse",
            "422": "ErrorResponse",
            "503": "ErrorResponse",
        },
    },
    "tycho.openapi.json": {
        ("get", "/v1/health"): {"200": "Health"},
        ("post", "/v1/tokens"): {"200": "TokensRequestResponse"},
    },
}

REQUEST_BODIES: dict[str, dict[tuple[str, str], str]] = {
    "fynd.openapi.json": {("post", "/v1/quote"): "QuoteRequest"},
    "tycho.openapi.json": {("post", "/v1/tokens"): "TokensRequestBody"},
}

# Every property the SDK sends or reads, by schema. Tycho's `Health` is a
# discriminated oneOf with no top-level properties and is checked separately.
CONTRACT_FIELDS: dict[str, dict[str, set[str]]] = {
    "fynd.openapi.json": {
        "QuoteRequest": {"orders", "options"},
        "Order": {"token_in", "token_out", "amount", "side", "sender", "receiver"},
        "QuoteOptions": {"timeout_ms", "min_responses", "max_gas", "encoding_options"},
        "EncodingOptions": {"slippage", "transfer_type"},
        "Quote": {"orders", "total_gas_estimate", "solve_time_ms"},
        "OrderQuote": {
            "order_id",
            "status",
            "amount_in",
            "amount_out",
            "amount_out_net_gas",
            "gas_estimate",
            "block",
            "gas_price",
            "price_impact_bps",
            "route",
            "transaction",
            "fee_breakdown",
        },
        "BlockInfo": {"number", "hash", "timestamp"},
        "Route": {"swaps"},
        "Swap": {
            "component_id",
            "protocol",
            "token_in",
            "token_out",
            "amount_in",
            "amount_out",
            "gas_estimate",
            "split",
        },
        "FeeBreakdown": {
            "router_fee",
            "client_fee",
            "max_slippage",
            "min_amount_received",
            "swaps_hash",
        },
        "Transaction": {"to", "value", "data", "client_fee_signature_offset"},
        "HealthStatus": {
            "healthy",
            "last_update_ms",
            "num_solver_pools",
            "derived_data_ready",
            "gas_price_age_ms",
        },
        "InstanceInfo": {"chain_id", "permit2_address", "router_address", "version"},
        "ErrorResponse": {"error", "code", "details"},
    },
    "tycho.openapi.json": {
        "TokensRequestBody": {
            "chain",
            "min_quality",
            "traded_n_days_ago",
            "token_addresses",
            "pagination",
        },
        "PaginationParams": {"page", "page_size"},
        "TokensRequestResponse": {"tokens", "pagination"},
        "ResponseToken": {"chain", "address", "symbol", "decimals", "tax", "gas", "quality"},
        "PaginationResponse": {"page", "page_size", "total"},
    },
}

# Response properties the SDK models as non-optional. If upstream drops one from
# `required`, every response missing it raises a ValidationError at runtime.
GUARANTEED_FIELDS: dict[str, dict[str, set[str]]] = {
    "fynd.openapi.json": {
        "Quote": {"orders", "total_gas_estimate", "solve_time_ms"},
        "OrderQuote": {
            "order_id",
            "status",
            "amount_in",
            "amount_out",
            "amount_out_net_gas",
            "gas_estimate",
            "block",
        },
        "BlockInfo": {"number", "hash", "timestamp"},
        "Route": {"swaps"},
        "Swap": {
            "component_id",
            "protocol",
            "token_in",
            "token_out",
            "amount_in",
            "amount_out",
            "gas_estimate",
            "split",
        },
        "FeeBreakdown": {"router_fee", "client_fee", "max_slippage", "min_amount_received"},
        "Transaction": {"to", "value", "data"},
        "HealthStatus": {"healthy", "last_update_ms", "num_solver_pools"},
        "InstanceInfo": {"chain_id", "permit2_address"},
        "ErrorResponse": {"error", "code"},
    },
    "tycho.openapi.json": {
        "TokensRequestResponse": {"tokens", "pagination"},
        "ResponseToken": {"chain", "address", "symbol", "decimals", "tax", "gas", "quality"},
        "PaginationResponse": {"page", "page_size", "total"},
    },
}


def load_spec(filename: str) -> dict[str, Any]:
    return json.loads((SPECS / filename).read_text())


def schema_of(spec: dict[str, Any], name: str) -> dict[str, Any]:
    schemas = spec["components"]["schemas"]
    assert name in schemas, f"schema {name} is gone from the spec"
    return schemas[name]


def operation_of(spec: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    return spec["paths"][path][method]


def referenced_schema_name(container: dict[str, Any]) -> str | None:
    ref = container.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
    return None if ref is None else ref.rsplit("/", 1)[-1]


def string_enum_values(schema: dict[str, Any]) -> set[str]:
    """Members of a schema rendered either as one string enum or as a `oneOf` of
    single-member string enums (how the Rust servers serialise their enums)."""
    values = set(schema.get("enum", []))
    for variant in schema.get("oneOf", []):
        values.update(variant.get("enum", []))
    return values


def remediation(filename: str) -> str:
    pin = PINS[filename]
    return f"Re-vendor {filename} from {pin['url']} and update {pin['models']} to match."


@pytest.mark.parametrize("filename", list(PINS))
def test_vendored_spec_version_pinned(filename: str) -> None:
    spec = load_spec(filename)
    assert spec["info"]["version"] == PINS[filename]["version"]


@pytest.mark.parametrize("filename", list(PINS))
def test_endpoints_the_sdk_calls_still_exist(filename: str) -> None:
    spec = load_spec(filename)
    missing = [
        f"{method.upper()} {path}"
        for method, path in ENDPOINT_RESPONSES[filename]
        if method not in spec["paths"].get(path, {})
    ]
    assert not missing, (
        f"{filename} no longer serves {missing}, which the SDK client calls. "
        f"{remediation(filename)}"
    )


@pytest.mark.parametrize("filename", list(PINS))
def test_response_schemas_are_unchanged(filename: str) -> None:
    spec = load_spec(filename)
    for (method, path), expected in ENDPOINT_RESPONSES[filename].items():
        responses = operation_of(spec, method, path)["responses"]
        for status, schema_name in expected.items():
            assert status in responses, (
                f"{filename}: {method.upper()} {path} no longer documents a {status} response, "
                f"which the SDK client handles. {remediation(filename)}"
            )
            actual = referenced_schema_name(responses[status])
            assert actual == schema_name, (
                f"{filename}: {method.upper()} {path} {status} now returns {actual}, "
                f"but the SDK parses it as {schema_name}. {remediation(filename)}"
            )


@pytest.mark.parametrize("filename", list(PINS))
def test_request_body_schemas_are_unchanged(filename: str) -> None:
    spec = load_spec(filename)
    for (method, path), schema_name in REQUEST_BODIES[filename].items():
        actual = referenced_schema_name(operation_of(spec, method, path)["requestBody"])
        assert actual == schema_name, (
            f"{filename}: {method.upper()} {path} now accepts {actual}, "
            f"but the SDK sends a {schema_name}. {remediation(filename)}"
        )


@pytest.mark.parametrize("filename", list(PINS))
def test_fields_the_sdk_uses_still_exist(filename: str) -> None:
    spec = load_spec(filename)
    for schema_name, fields in CONTRACT_FIELDS[filename].items():
        declared = set(schema_of(spec, schema_name)["properties"])
        missing = sorted(fields - declared)
        assert not missing, (
            f"{filename}: {schema_name} no longer declares {missing}, which the SDK "
            f"sends or reads by name. {remediation(filename)}"
        )


@pytest.mark.parametrize("filename", list(PINS))
def test_fields_the_sdk_treats_as_mandatory_are_still_required(filename: str) -> None:
    spec = load_spec(filename)
    for schema_name, fields in GUARANTEED_FIELDS[filename].items():
        required = set(schema_of(spec, schema_name).get("required", []))
        optional_now = sorted(fields - required)
        assert not optional_now, (
            f"{filename}: {schema_name} fields {optional_now} are no longer required upstream, "
            f"but the SDK models them as mandatory and will raise on any response omitting them. "
            f"Make it optional in {PINS[filename]['models']}."
        )


def test_fynd_still_accepts_the_request_enum_values_the_sdk_sends() -> None:
    spec = load_spec("fynd.openapi.json")
    assert "sell" in string_enum_values(schema_of(spec, "OrderSide")), (
        "Fynd no longer accepts side='sell'; every quote the SDK builds sends it."
    )
    assert "transfer_from" in string_enum_values(schema_of(spec, "UserTransferType")), (
        "Fynd no longer accepts transfer_type='transfer_from'; FyndClient.quote sends it "
        "whenever encoding is on."
    )


def test_fynd_quote_status_still_declares_the_statuses_the_sdk_knows() -> None:
    declared = string_enum_values(schema_of(load_spec("fynd.openapi.json"), "QuoteStatus"))
    assert QUOTE_STATUS_SUCCESS in declared, (
        f"Fynd no longer reports {QUOTE_STATUS_SUCCESS!r}; sweep, sizing and collect all treat "
        f"any other status as a failed order and would discard every quote."
    )
    dropped = sorted(KNOWN_QUOTE_STATUSES - declared)
    assert not dropped, (
        f"Fynd dropped quote statuses {dropped}; remove them from KNOWN_QUOTE_STATUSES in "
        f"{PINS['fynd.openapi.json']['models']}."
    )


def test_fynd_slippage_example_is_still_a_decimal_string() -> None:
    # The single most load-bearing undocumented wire fact: Fynd's deserializer
    # rejects a JSON number here. The schema says `number` and only the example
    # carries the truth, so the example is what this asserts.
    encoding_options = schema_of(load_spec("fynd.openapi.json"), "EncodingOptions")
    slippage = encoding_options["properties"]["slippage"]
    assert isinstance(slippage.get("example"), str), (
        "Fynd's EncodingOptions.slippage example is no longer a string. The SDK sends slippage as "
        "a decimal string because the server rejects a JSON float; re-verify against a live Fynd "
        "before changing EncodingOptions.slippage in "
        f"{PINS['fynd.openapi.json']['models']}."
    )


def test_tycho_health_still_declares_the_states_the_sdk_knows() -> None:
    variants = schema_of(load_spec("tycho.openapi.json"), "Health")["oneOf"]
    declared = {
        value
        for variant in variants
        for value in variant.get("properties", {}).get("status", {}).get("enum", [])
    }
    assert HEALTH_STATUS_READY in declared, (
        f"Tycho no longer reports status={HEALTH_STATUS_READY!r}; Health.ready would never be "
        f"true and any readiness poll would hang."
    )
    dropped = sorted(KNOWN_HEALTH_STATUSES - declared)
    assert not dropped, (
        f"Tycho dropped health states {dropped}; remove them from KNOWN_HEALTH_STATUSES in "
        f"{PINS['tycho.openapi.json']['models']}."
    )


def test_tycho_still_offers_every_chain_the_sdk_can_select() -> None:
    declared = string_enum_values(schema_of(load_spec("tycho.openapi.json"), "Chain"))
    dropped = sorted(set(get_args(Chain)) - declared)
    assert not dropped, (
        f"Tycho no longer supports chains {dropped}; a TychoClient bound to one would send a "
        f"chain the server rejects. Update Chain in {PINS['tycho.openapi.json']['models']}."
    )


def test_tycho_api_key_still_goes_in_the_authorization_header() -> None:
    scheme = load_spec("tycho.openapi.json")["components"]["securitySchemes"]["apiKey"]
    assert (scheme["in"], scheme["name"]) == ("header", "authorization"), (
        "Tycho moved its API key off the `authorization` header; TychoClient sets that header "
        "on every request and would start getting 401s."
    )


@pytest.mark.skipif(
    os.environ.get(LIVE_CHECK_ENV) != "1",
    reason=f"live upstream spec check is opt-in: set {LIVE_CHECK_ENV}=1",
)
@pytest.mark.parametrize("filename", list(PINS))
def test_vendored_spec_matches_upstream(filename: str) -> None:
    """Drift alarm for a scheduled job, not for `pytest`. A version bump here is
    informational — the contract checks above decide whether it breaks the SDK.

    Network failure fails the test rather than skipping: the run was asked for
    explicitly, so a silent pass would defeat the point.
    """
    url = PINS[filename]["url"]
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    upstream_version = response.json()["info"]["version"]
    assert upstream_version == PINS[filename]["version"], (
        f"{filename} drifted: vendored {PINS[filename]['version']}, upstream {upstream_version}. "
        f"Re-vendor from {url}; the contract tests in this file say whether anything the SDK "
        f"depends on actually moved."
    )
