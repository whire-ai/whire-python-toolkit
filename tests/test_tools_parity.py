"""``whire.tools`` must match the server's ``tools/list`` field by field (SPEC §7).

The only permitted difference is a ``description`` string added to input properties. Everything
else is compared verbatim against ``tests/fixtures/tools_list.json`` in ONE loop that reports
every mismatch at once.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from whire import READ_ONLY_TOOLS as EXPORTED_READ_ONLY_TOOLS
from whire.tools import (
    AMOUNT_PROPERTIES,
    DESTRUCTIVE_TOOLS,
    HELPER_TOOLS,
    LIST_TOOL_WRAPPERS,
    PROPERTY_DESCRIPTIONS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
    TOOLS,
    TOOLS_BY_NAME,
    WAIT_FOR_PAYOUT_TOOL,
    get_tool,
)
from whire.toolkit import WhireToolkit

SERVER_TOOLS: list[dict[str, Any]] = json.loads((Path(__file__).parent / "fixtures" / "tools_list.json").read_text())["result"]["tools"]


def strip_descriptions(schema: Any) -> Any:
    """Remove every ``description`` key at any depth of a JSON schema."""
    if isinstance(schema, dict):
        return {key: strip_descriptions(value) for key, value in schema.items() if key != "description"}
    if isinstance(schema, list):
        return [strip_descriptions(item) for item in schema]
    return schema


def test_tools_match_the_server_field_by_field() -> None:
    mismatches: list[str] = []
    assert [tool["name"] for tool in TOOLS] == [tool["name"] for tool in SERVER_TOOLS] == list(TOOL_NAMES) and len(TOOLS) == 33
    rendered = {tool["name"]: tool for tool in WhireToolkit(api_key="k").get_tools("mcp")}
    for ours, server in zip(TOOLS, SERVER_TOOLS, strict=True):
        name = server["name"]
        checks = {
            "keys": set(ours) == {"name", "title", "description", "input_schema", "output_schema", "annotations", "execution"},
            "title": ours["title"] == server["title"],
            "description": ours["description"] == server["description"],
            "output_schema": ours["output_schema"] == server["outputSchema"],
            "annotations": ours["annotations"] == server.get("annotations"),
            "execution": ours["execution"] == server.get("execution") == {"taskSupport": "forbidden"},
            # identical input schema once the added property descriptions are removed (the server ships none)
            "input_schema": strip_descriptions(ours["input_schema"]) == server["inputSchema"] == strip_descriptions(server["inputSchema"]),
            "required": ours["input_schema"].get("required", []) == server["inputSchema"].get("required", []),
            "property_descriptions": all(
                isinstance(spec.get("description"), str) and spec["description"].strip()
                for spec in (ours["input_schema"].get("properties") or {}).values()
            ),
            # the ``mcp`` rendering is the server's own object plus those descriptions
            "mcp_format": dict(rendered[name], inputSchema=strip_descriptions(rendered[name]["inputSchema"])) == server,
        }
        mismatches += [f"{name}: {field}" for field, passed in checks.items() if not passed]
    assert not mismatches, "tool definitions differ from the server:\n" + "\n".join(mismatches)
    with_inputs = {tool["name"] for tool in TOOLS if tool["input_schema"].get("properties")}
    assert with_inputs == set(PROPERTY_DESCRIPTIONS)
    assert set(TOOL_NAMES) - with_inputs == {"list_payers", "get_capabilities", "list_users", "list_payment_mandates", "list_payouts", "get_simulation"}


def test_derived_sets() -> None:
    assert DESTRUCTIVE_TOOLS == {"execute_payout", "pay_x402_resource"}
    for tool in TOOLS:
        destructive = tool["name"] in DESTRUCTIVE_TOOLS
        assert tool["annotations"] == ({"destructiveHint": True, "idempotentHint": False, "openWorldHint": True} if destructive else None), tool["name"]
    assert READ_ONLY_TOOLS is EXPORTED_READ_ONLY_TOOLS and not READ_ONLY_TOOLS & DESTRUCTIVE_TOOLS
    assert READ_ONLY_TOOLS == {
        "get_payer",
        "list_payers",
        "get_capabilities",
        "get_user",
        "list_users",
        "validate_iban",
        "get_beneficiary",
        "validate_payment_mandate",
        "list_payment_mandates",
        "get_payout_status",
        "list_payouts",
        "get_simulation",
        "quote_x402_resource",
        "verify_authorization_receipt",
        "authorize_payment",
    }
    assert LIST_TOOL_WRAPPERS == {"list_payers": "payers", "list_users": "users", "list_payment_mandates": "mandates", "list_payouts": "payouts"}
    for name, wrapper in LIST_TOOL_WRAPPERS.items():
        output = TOOLS_BY_NAME[name]["output_schema"]
        assert output["required"] == [wrapper] and output["properties"][wrapper]["type"] == "array"
    number_props = {
        (tool["name"], prop)
        for tool in TOOLS
        for prop, spec in (tool["input_schema"].get("properties") or {}).items()
        if spec.get("type") == "number"
    }
    assert {prop for _, prop in number_props} == set(AMOUNT_PROPERTIES)
    assert number_props == {
        ("authorize_payment", "amount"),
        ("create_payment_mandate", "maxAmount"),
        ("create_payment_mandate", "maxTotalAmount"),
        ("validate_payment_mandate", "amount"),
        ("create_payout_draft", "amount"),
    }


def test_wait_for_payout_helper_definition_and_immutability() -> None:
    assert HELPER_TOOLS == [WAIT_FOR_PAYOUT_TOOL] and WAIT_FOR_PAYOUT_TOOL["name"] == "wait_for_payout" and "wait_for_payout" not in TOOL_NAMES
    assert "not offered by the hosted MCP server" in WAIT_FOR_PAYOUT_TOOL["description"]
    schema = WAIT_FOR_PAYOUT_TOOL["input_schema"]
    assert schema["required"] == ["payoutId"] and set(schema["properties"]) == {"payoutId", "untilStatuses", "timeoutSeconds"}
    assert schema["properties"]["untilStatuses"]["type"] == "array" and schema["properties"]["timeoutSeconds"]["maximum"] == 60
    assert WAIT_FOR_PAYOUT_TOOL["output_schema"] == TOOLS_BY_NAME["get_payout_status"]["output_schema"]
    assert WAIT_FOR_PAYOUT_TOOL["output_schema"] is not TOOLS_BY_NAME["get_payout_status"]["output_schema"]
    assert WAIT_FOR_PAYOUT_TOOL["annotations"]["destructiveHint"] is False
    assert get_tool("wait_for_payout") is WAIT_FOR_PAYOUT_TOOL and get_tool("get_payer") is TOOLS_BY_NAME["get_payer"]
    with pytest.raises(KeyError):
        get_tool("nope")
    before = copy.deepcopy(TOOLS)
    toolkit = WhireToolkit(api_key="k")
    for fmt in ("openai", "openai-responses", "anthropic", "mcp"):
        for entry in toolkit.get_tools(fmt, include_helpers=True):  # type: ignore[arg-type]
            entry.clear()
    assert TOOLS == before  # every rendering is a deep copy; TOOLS is never mutated
