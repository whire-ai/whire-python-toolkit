"""``WhireToolkit``: tool formats, argument normalisation, gating, dispatch, error dicts and hints (SPEC §7)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import jsonschema
import pytest

from tests.conftest import DEFAULT_API_KEY, RecordingTransport, answers, fail, make_client, ok, tool_failure, tool_reply_for
from tests.fixtures import payloads as P
from whire import WhireClient, WhireToolkit
from whire.exceptions import PayoutExecutionRefused
from whire.models import Capabilities
from whire.prompts import SYSTEM_PROMPT
from whire.tools import DESTRUCTIVE_TOOLS, LIST_TOOL_WRAPPERS, TOOL_NAMES, TOOLS_BY_NAME, WAIT_FOR_PAYOUT_TOOL
from whire.toolkit import DO_NOT_RESEND_HINT

ERROR_KEYS = {"error", "error_code", "status_code", "retryable", "needs_user_action", "is_input_error", "suggestion", "request_id", "idempotency_key"}
URL = "http://127.0.0.1:4604/report"


def toolkit_for(recording: RecordingTransport, **kwargs: Any) -> WhireToolkit:
    """A toolkit around a ``make_client`` client (max_retries=0, sandbox, test key)."""
    return WhireToolkit(client=make_client(recording), **kwargs)


def execute_handler(request: httpx.Request) -> httpx.Response:
    """The execute_payout confirmation summary reads the payout (REST) and the beneficiary (MCP); execute answers paid."""
    if request.url.path == "/mcp":
        return tool_reply_for(request, P.fresh(P.BENEFICIARY))
    return ok(P.fresh(P.PAYOUT_APPROVED)) if request.method == "GET" else ok(P.fresh(P.EXECUTION_PAID))


def _no_requests(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"hint() must not send requests ({request.method} {request.url})")


# --------------------------------------------------------------------------- construction, lifecycle, get_tools


async def test_construction_lifecycle_and_repr(recording: RecordingTransport) -> None:
    client = make_client()
    for kwargs in ({"api_key": "k"}, {"environment": "sandbox"}, {"base_url": "https://sandbox.whire.ai"}, {"timeout": 5.0}, {"transport": recording}):
        with pytest.raises(TypeError, match="either client= or connection settings"):
            WhireToolkit(client, **kwargs)
    with pytest.raises(TypeError, match="callable"):
        WhireToolkit(api_key="k", confirm="yes")  # type: ignore[arg-type]
    toolkit = WhireToolkit(api_key="secret-key-wxyz", transport=recording, confirm=lambda *_: True)
    assert isinstance(toolkit.client, WhireClient) and toolkit.client.base_url == "https://sandbox.whire.ai"
    assert "secret-key" not in repr(toolkit) and "…wxyz" in repr(toolkit) and "confirm='set'" in repr(toolkit)
    await toolkit.close()
    await toolkit.close()  # idempotent; an owned client is closed
    assert toolkit.client.is_closed
    async with WhireToolkit(client=client) as borrowed:
        assert borrowed.client is client
    assert not client.is_closed  # a passed client stays open
    await client.close()
    plain = WhireToolkit(api_key="k")
    assert plain.allow_destructive is False and plain.confirm is None and plain.require_confirmation == DESTRUCTIVE_TOOLS
    assert plain.destructive_tools == {"execute_payout", "pay_x402_resource"} and plain.tool_names == TOOL_NAMES
    assert plain.system_prompt is SYSTEM_PROMPT and plain.capabilities is None


def test_get_tools_formats_filters_and_copies() -> None:
    toolkit = WhireToolkit(api_key="k")
    for fmt in ("openai", "openai-responses", "anthropic", "mcp"):
        tools = toolkit.get_tools(fmt)  # type: ignore[arg-type]
        assert len(tools) == 33 and [t["function"]["name"] if fmt == "openai" else t["name"] for t in tools] == list(TOOL_NAMES)
    with pytest.raises(ValueError, match="format"):
        toolkit.get_tools("gemini")  # type: ignore[arg-type]
    chat = toolkit.get_tools("openai", names=["create_payout_draft"])[0]
    assert set(chat) == {"type", "function"} and chat["type"] == "function" and set(chat["function"]) == {"name", "description", "parameters"}
    params = chat["function"]["parameters"]
    assert "$schema" not in params and params["required"] == ["beneficiaryId", "amount", "currency", "reason"]
    assert params["properties"]["amount"]["type"] == "number" and "two decimals" in params["properties"]["amount"]["description"]
    assert toolkit.get_tools("openai", names=["list_payers"])[0]["function"]["parameters"] == {"type": "object", "properties": {}}
    responses = toolkit.get_tools("openai-responses", names=["get_payer"])[0]
    assert set(responses) == {"type", "name", "description", "parameters"} and "$schema" not in responses["parameters"]
    anthropic = toolkit.get_tools("anthropic", names=["get_payer"])[0]
    assert set(anthropic) == {"name", "description", "input_schema"} and "$schema" not in anthropic["input_schema"]
    mcp = {t["name"]: t for t in toolkit.get_tools("mcp")}
    assert set(mcp["execute_payout"]) == {"name", "title", "description", "inputSchema", "outputSchema", "annotations", "execution"}
    assert mcp["execute_payout"]["inputSchema"]["$schema"] == "http://json-schema.org/draft-07/schema#" and "annotations" not in mcp["get_payer"]
    assert [t["name"] for t in toolkit.get_tools("anthropic", names={"list_payouts", "get_payer"})] == ["get_payer", "list_payouts"]  # server order
    with pytest.raises(ValueError, match="unknown tool"):
        toolkit.get_tools(names=["nope"])
    names = {t["name"] for t in toolkit.get_tools("anthropic", exclude_destructive=True)}
    assert len(names) == 31 and not names & DESTRUCTIVE_TOOLS
    with_helpers = toolkit.get_tools("mcp", include_helpers=True)
    assert len(with_helpers) == 34 and with_helpers[-1]["name"] == "wait_for_payout" and "execution" not in with_helpers[-1]
    assert "wait_for_payout" not in mcp and len(toolkit.get_tools("openai", names=["wait_for_payout"], include_helpers=True)) == 1
    with pytest.raises(ValueError):
        toolkit.get_tools("openai", names=["wait_for_payout"])
    rendered = toolkit.get_tools("mcp", names=["execute_payout"])[0]
    rendered["annotations"]["destructiveHint"] = False
    assert TOOLS_BY_NAME["execute_payout"]["annotations"]["destructiveHint"] is True  # deep copies, never the definitions


# --------------------------------------------------------------------------- dispatch: every tool

_FS = {"type": "sepa", "destination": P.PAYER_IBAN}
_PAYER_ARGS = {"legalName": "Merchant B.V.", "contactFirstName": "Eva", "contactLastName": "Jansen", "email": "finance@merchant.example", "phone": "+31612345678", "fundingSources": [_FS]}
_USER_ARGS = {"firstName": "Smoke", "lastName": "User", "email": "user@example.com", "phone": "+31698765432", "paymentMethods": [{"type": "sepa", "destination": P.USER_IBAN}]}
_MANDATE_ARGS = {
    "beneficiaryId": P.BENEFICIARY_ID, "mandateReference": "SHOP-001", "payerId": P.PAYER_ID, "signedBy": "Finance", "currency": "EUR",
    "maxAmount": 100, "maxTotalAmount": 250, "scheme": "agent_payout", "mandateType": "recurring",
}

# tool -> (arguments, expected HTTP method, expected path); "MCP" means a tools/call of that tool.
DISPATCH: dict[str, tuple[dict[str, Any], str, str]] = {
    "register_payer": (_PAYER_ARGS, "POST", "/api/payers"),
    "activate_payer_account": ({"payerId": P.PAYER_ID, "verifiedBy": "Compliance"}, "POST", f"/api/payers/{P.PAYER_ID}/activate"),
    "suspend_payer_account": ({"payerId": P.PAYER_ID, "reason": "Chargebacks"}, "POST", f"/api/payers/{P.PAYER_ID}/suspend"),
    "add_funding_source": ({"payerId": P.PAYER_ID, "type": "sepa", "destination": P.PAYER_IBAN_2, "makeDefault": True}, "POST", f"/api/payers/{P.PAYER_ID}/funding-sources"),
    "set_default_funding_source": ({"payerId": P.PAYER_ID, "sourceId": P.SOURCE_ID_2}, "MCP", "set_default_funding_source"),
    "get_payer": ({"payerId": P.PAYER_ID}, "GET", f"/api/payers/{P.PAYER_ID}"),
    "list_payers": ({}, "GET", "/api/payers"),
    "authorize_payment": ({"mandateId": P.MANDATE_ID, "amount": 50, "currency": "EUR", "beneficiaryId": P.BENEFICIARY_ID}, "POST", "/api/authorize"),
    "verify_authorization_receipt": ({"receipt": P.RECEIPT}, "POST", "/api/authorize/verify"),
    "get_capabilities": ({}, "GET", "/api/capabilities"),
    "register_user": (_USER_ARGS, "MCP", "register_user"),
    "add_payment_method": ({"userId": P.USER_ID, "type": "sepa", "destination": P.PAYER_IBAN_2}, "MCP", "add_payment_method"),
    "set_default_payment_method": ({"userId": P.USER_ID, "methodId": P.METHOD_ID}, "MCP", "set_default_payment_method"),
    "get_user": ({"userId": P.USER_ID}, "MCP", "get_user"),
    "list_users": ({}, "MCP", "list_users"),
    "create_beneficiary_for_user": ({"userId": P.USER_ID}, "MCP", "create_beneficiary_for_user"),
    "validate_iban": ({"iban": "DE89 3704 0044 0532 0130 00"}, "POST", "/api/validate-iban"),
    "create_beneficiary": ({"fullName": "Acme Supplies BV", "iban": P.BENEFICIARY_IBAN, "reference": "acme"}, "POST", "/api/beneficiaries"),
    "get_beneficiary": ({"beneficiaryId": P.BENEFICIARY_ID}, "MCP", "get_beneficiary"),
    "create_payment_mandate": (_MANDATE_ARGS, "POST", "/api/mandates"),
    "validate_payment_mandate": ({"mandateId": P.MANDATE_ID, "amount": 50, "currency": "EUR", "beneficiaryId": P.BENEFICIARY_ID}, "POST", f"/api/mandates/{P.MANDATE_ID}/validate"),
    "revoke_payment_mandate": ({"mandateId": P.MANDATE_ID, "reason": "Withdrawn"}, "POST", f"/api/mandates/{P.MANDATE_ID}/revoke"),
    "list_payment_mandates": ({}, "GET", "/api/mandates"),
    "create_payout_draft": ({"beneficiaryId": P.BENEFICIARY_ID, "amount": 50, "currency": "EUR", "reason": "Invoice 2026-091", "mandateId": P.MANDATE_ID}, "POST", "/api/payouts"),
    "submit_payout_for_review": ({"payoutId": P.PAYOUT_ID}, "POST", f"/api/payouts/{P.PAYOUT_ID}/submit"),
    "record_provider_event": ({"payoutId": P.PAYOUT_ID_NO_MANDATE, "event": "kyc_approved"}, "POST", f"/api/payouts/{P.PAYOUT_ID_NO_MANDATE}/events"),
    "confirm_payout": ({"payoutId": P.PAYOUT_ID}, "POST", f"/api/payouts/{P.PAYOUT_ID}/confirm"),
    "execute_payout": ({"payoutId": P.PAYOUT_ID}, "POST", f"/api/payouts/{P.PAYOUT_ID}/execute"),
    "get_payout_status": ({"payoutId": P.PAYOUT_ID}, "GET", f"/api/payouts/{P.PAYOUT_ID}"),
    "list_payouts": ({}, "GET", "/api/payouts"),
    "get_simulation": ({}, "GET", "/api/simulation"),
    "quote_x402_resource": ({"url": URL}, "MCP", "quote_x402_resource"),
    "pay_x402_resource": ({"url": URL, "mandateId": P.MANDATE_ID}, "MCP", "pay_x402_resource"),
}


async def test_every_tool_dispatches_to_the_right_call_and_returns_the_verbatim_payload() -> None:
    assert set(DISPATCH) == set(TOOL_NAMES)
    problems: list[str] = []
    for name, (arguments, method, target) in DISPATCH.items():
        recording = RecordingTransport()
        recording.handler = answers(name)
        result = await toolkit_for(recording, allow_destructive=True).execute(name, arguments)
        expected = P.TOOL_RESULTS[name]
        if result != expected:
            problems.append(f"{name}: result {result!r} != fixture")
        if name in LIST_TOOL_WRAPPERS and list(result) != [LIST_TOOL_WRAPPERS[name]]:
            problems.append(f"{name}: list wrapper {list(result)}")
        if len(recording) != 1:
            problems.append(f"{name}: {len(recording)} requests")
            continue
        request = recording.last
        if method == "MCP":
            if request.path != "/mcp" or request.json["method"] != "tools/call" or request.tool_call[0] != target:
                problems.append(f"{name}: expected tools/call {target}, sent {request.method} {request.path}")
        elif (request.method, request.path) != (method, target) or request.headers.get("x-api-key") != DEFAULT_API_KEY:
            problems.append(f"{name}: expected {method} {target}, sent {request.method} {request.path}")
        elif method == "POST" and not request.idempotency_key:
            problems.append(f"{name}: REST POST without an Idempotency-Key")
    assert not problems, "\n".join(problems)


async def test_every_result_validates_against_the_tools_output_schema() -> None:
    problems: list[str] = []
    for name, (arguments, _, _) in DISPATCH.items():
        recording = RecordingTransport()
        recording.handler = answers(name)
        result = await toolkit_for(recording, allow_destructive=True).execute(name, arguments)
        errors = list(jsonschema.Draft7Validator(TOOLS_BY_NAME[name]["output_schema"]).iter_errors(result))
        problems += [f"{name}: {error.message}" for error in errors]
    assert not problems, "\n".join(problems)


async def test_list_status_none_arguments_receipts_and_no_added_keys(recording: RecordingTransport) -> None:
    recording.handler = answers("list_payouts")
    toolkit = toolkit_for(recording)
    assert await toolkit.execute("list_payouts", {"status": "paid"}) == P.PAYOUT_LIST_PAID and recording.last.query == {"status": "paid"}
    assert (await toolkit.execute("list_payouts", {"status": "PAID"}))["error_code"] == "invalid_input"
    recording.handler = answers("get_capabilities")
    assert await toolkit.execute("get_capabilities", None) == P.CAPABILITIES  # None arguments are accepted
    recording.handler = answers("get_payout_status")
    result = await toolkit.execute("get_payout_status", {"payoutId": P.PAYOUT_ID})
    assert set(result) == set(P.PAYOUT_APPROVED)  # never adds keys (every outputSchema is additionalProperties: false)
    receipt = {**P.RECEIPT, "extra": {"anything": [1, 2.5, None]}}  # extra keys must survive untouched
    recording.handler = answers("verify_authorization_receipt")
    assert await toolkit.execute("verify_authorization_receipt", {"receipt": receipt}) == P.RECEIPT_VERIFICATION
    assert recording.last.body == json.dumps({"receipt": receipt}, separators=(",", ":"), ensure_ascii=False).encode()
    assert (await toolkit.execute("verify_authorization_receipt", {"receipt": "auth_123"}))["error_code"] == "invalid_input"


async def test_execute_payout_goes_through_rest_with_a_key_and_refusals_carry_the_hint(recording: RecordingTransport) -> None:
    recording.handler = answers("execute_payout")
    toolkit = toolkit_for(recording, allow_destructive=True)
    assert await toolkit.execute("execute_payout", {"payoutId": P.PAYOUT_ID}) == P.EXECUTION_PAID
    assert (recording.last.method, recording.last.path, recording.last.json) == ("POST", f"/api/payouts/{P.PAYOUT_ID}/execute", {})
    assert recording.last.idempotency_key and len(recording.last.idempotency_key) == 36
    await toolkit.execute("execute_payout", {"payout_id": P.PAYOUT_ID, "idempotency_key": "retry-0001"})
    assert recording.last.idempotency_key == "retry-0001"  # a caller-supplied key is honoured
    off = RecordingTransport()
    off.handler = answers("execute_payout")
    await WhireToolkit(client=make_client(off, auto_idempotency=False), allow_destructive=True).execute("execute_payout", {"payoutId": P.PAYOUT_ID})
    assert off.last.idempotency_key and uuid.UUID(off.last.idempotency_key).version == 4  # keyed even when the client is not auto-keyed
    recording.push(fail(P.ERROR_INSUFFICIENT_FUNDS, 400), fail(P.ERROR_ALREADY_EXECUTED, 400))
    result = await toolkit.execute("execute_payout", {"payoutId": P.PAYOUT_ID_FAILED})
    assert ERROR_KEYS <= set(result) and result["error"] == P.ERROR_INSUFFICIENT_FUNDS and result["error_code"] == "execution_refused"
    assert result["needs_user_action"] is True and result["retryable"] is False and result["is_input_error"] is False
    assert result["hint"] == DO_NOT_RESEND_HINT and result["idempotency_key"] == recording.last.idempotency_key
    assert PayoutExecutionRefused("x").to_agent_dict()["suggestion"] == result["suggestion"]
    already = await toolkit.execute("execute_payout", {"payoutId": P.PAYOUT_ID})
    assert already["error_code"] == "already_executed" and already["hint"] == DO_NOT_RESEND_HINT
    recording.push(tool_failure("Mandate does not cover 1.00 EUR to DE89370400440532013000."))
    paid = await toolkit.execute("pay_x402_resource", {"url": URL, "mandateId": P.MANDATE_ID})
    assert paid["error_code"] == "execution_refused" and paid["hint"] == DO_NOT_RESEND_HINT


# --------------------------------------------------------------------------- argument normalisation


async def test_argument_normalisation_and_local_validation(recording: RecordingTransport) -> None:
    recording.handler = answers("create_payout_draft")
    toolkit = toolkit_for(recording)
    arguments = {"beneficiary_id": P.BENEFICIARY_ID, "amount": "50", "currency": "EUR", "reason": "Invoice", "mandate_id": P.MANDATE_ID}
    assert await toolkit.execute("create_payout_draft", arguments) == P.PAYOUT_DRAFT
    assert recording.last.json == {"beneficiaryId": P.BENEFICIARY_ID, "mandateId": P.MANDATE_ID, "amount": 50, "currency": "EUR", "reason": "Invoice"}
    recording.handler = answers("get_payer")
    await toolkit.execute("get_payer", {"payer_id": "snake", "payerId": "camel", "verbose": True})  # extras ignored
    assert recording.last.path == "/api/payers/camel"  # camelCase wins when both spellings are given
    recording.handler = answers("register_payer")
    nested = {**_PAYER_ARGS, "fundingSources": [{"type": "sepa", "destination": P.PAYER_IBAN, "holder_name": "Merchant", "make_default": True}]}
    await toolkit.execute("register_payer", nested)
    assert recording.last.json["fundingSources"] == [{"type": "sepa", "destination": P.PAYER_IBAN, "holderName": "Merchant", "makeDefault": True}]
    recording.handler = answers("authorize_payment")
    for value, wire in (("50.00", 50), ("20.10", 20.1), (" 7.5 ", 7.5), (20.1, 20.1)):  # numeric strings coerced for number properties
        await toolkit.execute("authorize_payment", {"mandateId": P.MANDATE_ID, "amount": value, "beneficiaryId": P.BENEFICIARY_ID})
        assert recording.last.json["amount"] == wire and isinstance(recording.last.json["amount"], (int, float)), value
    recording.handler = answers("create_payment_mandate")
    await toolkit.execute("create_payment_mandate", {**_MANDATE_ARGS, "maxAmount": "100", "maxTotalAmount": "250.50"})
    assert recording.last.json["maxAmount"] == 100 and recording.last.json["maxTotalAmount"] == 250.5
    sent = len(recording)
    for value in ("50.123", "abc", 0, True, None, float("nan"), "1e15"):
        result = await toolkit.execute("create_payout_draft", {"beneficiaryId": P.BENEFICIARY_ID, "amount": value, "currency": "EUR", "reason": "x"})
        assert result["error_code"] == "invalid_input" and result["is_input_error"] is True and "amount" in result["error"], value
    missing = await toolkit.execute("activate_payer_account", {"payerId": P.PAYER_ID})
    assert missing["error_code"] == "invalid_input" and "verifiedBy" in missing["error"]  # named in camelCase
    assert (await toolkit.execute("get_payer", ["abc"]))["error_code"] == "invalid_input"  # type: ignore[arg-type]
    unknown = await toolkit.execute("send_money", {"amount": 1})
    assert unknown["error"] == "Unknown tool: send_money" and unknown["error_code"] == "unknown_tool" and ERROR_KEYS <= set(unknown)
    assert len(recording) == sent


# --------------------------------------------------------------------------- gating matrix


async def _async_yes(name: str, arguments: dict[str, Any], summary: dict[str, Any]) -> bool:
    await asyncio.sleep(0)
    return True


async def _async_no(name: str, arguments: dict[str, Any], summary: dict[str, Any]) -> bool:
    await asyncio.sleep(0)
    return False


CONFIRMS: dict[str, Any] = {"none": None, "sync_yes": lambda *_: True, "sync_no": lambda *_: False, "async_yes": _async_yes, "async_no": _async_no}


async def test_gating_matrix() -> None:
    """allow_destructive x confirm x require_confirmation, for both destructive tools."""
    for name in sorted(DESTRUCTIVE_TOOLS):
        for allow_destructive in (False, True):
            for confirm_name, confirm in CONFIRMS.items():
                for gated in (True, False):
                    recording = RecordingTransport()
                    recording.handler = execute_handler if name == "execute_payout" else answers(name)
                    kwargs: dict[str, Any] = {"allow_destructive": allow_destructive, "confirm": confirm}
                    if not gated:
                        kwargs["require_confirmation"] = frozenset()
                    result = await toolkit_for(recording, **kwargs).execute(name, DISPATCH[name][0])
                    case = (name, allow_destructive, confirm_name, gated)
                    executed = [r for r in recording.requests if r.path.endswith("/execute") or (r.path == "/mcp" and r.tool_call[0] == name)]
                    # allow_destructive=False is a floor: an empty require_confirmation does not ungate these tools
                    if confirm_name.endswith("yes") or (allow_destructive and (not gated or confirm_name == "none")):
                        assert result == P.TOOL_RESULTS[name] and len(executed) == 1, case
                    elif confirm_name == "none":
                        assert result["error_code"] == "confirmation_required" and "requires human confirmation" in result["error"], case
                        assert result["needs_user_action"] is True and result["is_input_error"] is False and result["retryable"] is False
                        assert result["suggestion"].startswith("Ask the user to approve") and len(recording) == 0  # not even the summary reads
                    else:
                        assert result["error"] == f"Confirmation declined for {name}" and result["error_code"] == "confirmation_declined", case
                        assert result["needs_user_action"] is True and ERROR_KEYS <= set(result) and executed == []


async def test_confirm_summaries_and_custom_require_confirmation(recording: RecordingTransport) -> None:
    seen: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    recording.handler = execute_handler
    toolkit = toolkit_for(recording, confirm=lambda n, a, s: seen.append((n, a, s)) or True)
    assert await toolkit.execute("execute_payout", {"payout_id": P.PAYOUT_ID}) == P.EXECUTION_PAID
    summary = {
        "payoutId": P.PAYOUT_ID, "amount": 50, "currency": "EUR", "status": "approved", "beneficiaryId": P.BENEFICIARY_ID,
        "counterpartyIban": P.BENEFICIARY_IBAN, "counterpartyName": "Acme Supplies BV", "mandateId": P.MANDATE_ID,
    }
    assert seen == [("execute_payout", {"payoutId": P.PAYOUT_ID}, summary)]  # arguments normalised before the human sees them
    assert [(r.method, r.path) for r in recording.requests] == [("GET", f"/api/payouts/{P.PAYOUT_ID}"), ("POST", "/mcp"), ("POST", f"/api/payouts/{P.PAYOUT_ID}/execute")]
    assert recording.requests[1].tool_call == ("get_beneficiary", {"beneficiaryId": P.BENEFICIARY_ID})
    missing = RecordingTransport()  # a missing beneficiary leaves the counterparty fields empty instead of blocking
    missing.handler = lambda request: tool_failure(P.ERROR_BENEFICIARY_NOT_FOUND)(request) if request.url.path == "/mcp" else execute_handler(request)
    seen.clear()
    assert await toolkit_for(missing, confirm=lambda n, a, s: seen.append((n, a, s)) or True).execute("execute_payout", {"payoutId": P.PAYOUT_ID}) == P.EXECUTION_PAID
    assert seen[0][2]["counterpartyIban"] is None and seen[0][2]["amount"] == 50
    failing = RecordingTransport().push(fail(P.ERROR_PAYOUT_NOT_FOUND, 400))  # a failing summary read is an error dict, confirm never runs
    seen.clear()
    result = await toolkit_for(failing, confirm=lambda n, a, s: seen.append((n, a, s)) or True).execute("execute_payout", {"payoutId": "nope"})
    assert result["error_code"] == "not_found" and seen == [] and len(failing) == 1
    quoted = RecordingTransport()
    quoted.handler = answers("quote_x402_resource")
    result = await toolkit_for(quoted, confirm=_async_no).execute("pay_x402_resource", {"url": URL, "mandateId": P.MANDATE_ID})
    assert result["error_code"] == "confirmation_declined" and [r.tool_call[0] for r in quoted.requests] == ["quote_x402_resource"]
    assert result["summary"] == {"url": URL, "amount": "1.00", "asset": "EUR", "payTo": P.BENEFICIARY_IBAN, "payeeName": "Report Vendor BV", "mandateId": P.MANDATE_ID}
    custom = RecordingTransport()
    custom.handler = answers("get_payer", rest=P.EXECUTION_PAID)
    seen.clear()
    toolkit = toolkit_for(custom, confirm=lambda n, a, s: seen.append((n, a, s)) or True, require_confirmation={"get_payer"})
    assert toolkit.require_confirmation == frozenset({"get_payer"})
    custom.handler = answers("get_payer")
    assert await toolkit.execute("get_payer", {"payer_id": P.PAYER_ID}) == P.PAYER_ACTIVE
    assert seen == [("get_payer", {"payerId": P.PAYER_ID}, {"payerId": P.PAYER_ID})]
    custom.handler = execute_handler
    assert await toolkit.execute("execute_payout", {"payoutId": P.PAYOUT_ID}) == P.EXECUTION_PAID
    assert seen[-1][0] == "execute_payout"  # still gated: narrowing require_confirmation never ungates without allow_destructive
    with pytest.raises(TypeError):  # a str would become a set of characters and silently ungate execute_payout
        toolkit_for(custom, require_confirmation="execute_payout")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        toolkit_for(custom, require_confirmation={"execute_payuot"})
    truthy = RecordingTransport()
    truthy.handler = execute_handler
    result = await toolkit_for(truthy, confirm=lambda n, a, s: "yes").execute("execute_payout", {"payoutId": P.PAYOUT_ID})  # type: ignore[arg-type,return-value]
    assert result["error_code"] == "confirmation_declined" and not [r for r in truthy.requests if r.path.endswith("/execute")]  # only exactly True runs


# --------------------------------------------------------------------------- error dicts and the wait_for_payout helper


async def test_error_dicts(recording: RecordingTransport) -> None:
    recording.push(fail(P.ERROR_PAYER_NOT_FOUND, 400, headers={"x-railway-request-id": "req-1"}))
    toolkit = toolkit_for(recording)
    result = await toolkit.execute("get_payer", {"payerId": "nope"})
    assert set(result) == ERROR_KEYS and result["error"] == P.ERROR_PAYER_NOT_FOUND and result["error_code"] == "not_found"
    assert result["status_code"] == 400 and result["is_input_error"] is True and result["retryable"] is False and result["request_id"] == "req-1"
    recording.push(tool_failure(P.MCP_ERROR_BUSINESS_NOT_FOUND))
    mcp = await toolkit.execute("get_user", {"userId": "nope"})
    assert mcp["error_code"] == "not_found" and mcp["status_code"] is None
    recording.push(httpx.ConnectError("no route"))
    network = await toolkit.execute("get_capabilities", {})
    assert network["error_code"] == "network_error" and network["retryable"] is True and "hint" not in network
    recording.push(httpx.ReadTimeout("stalled"))
    ambiguous = await toolkit.execute("register_user", _USER_ARGS)
    assert ambiguous["error_code"] == "ambiguous_outcome" and ambiguous["retryable"] is False and ambiguous["hint"] == DO_NOT_RESEND_HINT
    recording.push(fail("Invalid API key.", 401))
    auth = await toolkit.execute("get_payer", {"payerId": P.PAYER_ID})
    assert auth["error_code"] == "auth_failed" and DEFAULT_API_KEY not in json.dumps(auth)
    await toolkit.client.close()
    assert (await toolkit.execute("get_capabilities", {}))["error_code"] == "client_closed"


async def test_wait_for_payout_helper(recording: RecordingTransport, no_sleep: list[float], clock) -> None:
    recording.push(ok(P.fresh(P.PAYOUT_PENDING_KYC)), ok(P.fresh(P.PAYOUT_PENDING_KYC)), ok(P.fresh(P.PAYOUT_APPROVED)))
    toolkit = toolkit_for(recording)
    result = await toolkit.execute("wait_for_payout", {"payout_id": P.PAYOUT_ID})
    assert result == P.PAYOUT_APPROVED and len(recording) == 3 and all(r.method == "GET" for r in recording.requests) and len(no_sleep) == 2
    jsonschema.Draft7Validator(WAIT_FOR_PAYOUT_TOOL["output_schema"]).validate(result)
    recording.push(ok(P.fresh(P.PAYOUT_PROCESSING)), ok(P.fresh(P.PAYOUT_PAID)))
    assert await toolkit.execute("wait_for_payout", {"payoutId": P.PAYOUT_ID, "untilStatuses": ["paid", "failed"], "timeoutSeconds": "10"}) == P.PAYOUT_PAID
    sent = len(recording)
    for arguments in ({"payoutId": P.PAYOUT_ID, "timeoutSeconds": 0}, {"payoutId": P.PAYOUT_ID, "untilStatuses": ["nope"]}, {"payoutId": P.PAYOUT_ID, "untilStatuses": "paid"}):
        assert (await toolkit.execute("wait_for_payout", arguments))["error_code"] == "invalid_input", arguments
    assert len(recording) == sent

    def poll(request: httpx.Request) -> httpx.Response:
        clock.now += 5.0
        return ok(P.fresh(P.PAYOUT_PROCESSING))

    recording.handler = poll
    timeout = await toolkit.execute("wait_for_payout", {"payoutId": P.PAYOUT_ID, "timeoutSeconds": 8})
    assert timeout["error_code"] == "timeout" and timeout["retryable"] is False and timeout["last"] == P.PAYOUT_PROCESSING


# --------------------------------------------------------------------------- hint() and the system prompt


def test_hints_derived_from_results() -> None:
    toolkit = WhireToolkit(api_key="k", transport=httpx.MockTransport(_no_requests))
    for name, result, needles in (
        ("submit_payout_for_review", P.SUBMISSION, (P.SUBMISSION["nextAction"], "2-3 seconds")),
        ("execute_payout", P.EXECUTION_UNRESOLVED, ("Do not resend", "get_payout_status")),
        ("execute_payout", P.EXECUTION_PAID, ("SIM-5DC8B5E2",)),
        ("register_payer", P.PAYER_PENDING, ("activate_payer_account", "pending_verification")),
        ("get_payout_status", P.PAYOUT_DRAFT, ("submit_payout_for_review",)),
        ("get_payout_status", P.PAYOUT_APPROVED, ("execute_payout",)),
        ("get_payout_status", P.PAYOUT_PROCESSING, ("Do not resend",)),
        ("get_payout_status", P.PAYOUT_FAILED, ("terminal",)),
        ("get_payout_status", {**P.PAYOUT_PAID, "status": "returned"}, ("mandate total",)),
        ("authorize_payment", P.RECEIPT_REFUSED, ("per_payment_limit", "do not create a payout")),
        ("authorize_payment", P.RECEIPT, ("nothing was moved or reserved",)),
        ("verify_authorization_receipt", P.RECEIPT_VERIFICATION_TAMPERED, ("not usable",)),
        ("confirm_payout", P.TRANSITION_CONFIRMED, ("settles nothing",)),
        ("quote_x402_resource", P.X402_QUOTE_PAID, ("1.00 EUR", "human approval")),
        ("get_capabilities", P.CAPABILITIES_AUTH_ONLY, ("Not simulated", "refused")),
        ("validate_iban", P.IBAN_VALIDATION_INVALID, ("not valid",)),
        ("execute_payout", {"error": "x", "error_code": "confirmation_required", "suggestion": "Ask"}, ("human must approve",)),
        ("get_payer", {"error": "x", "error_code": "not_found", "suggestion": "Check the id"}, ("Check the id",)),
    ):
        hint = toolkit.hint(name, {}, result)
        assert hint is not None and all(needle in hint for needle in needles), (name, needles, hint)
    assert toolkit.hint("wait_for_payout", {}, P.PAYOUT_APPROVED) == toolkit.hint("get_payout_status", {}, P.PAYOUT_APPROVED)
    for name, result in (("validate_iban", P.IBAN_VALIDATION), ("list_payouts", P.PAYOUT_LIST_PAID), ("nope", {"x": 1}), ("get_payer", None)):
        assert toolkit.hint(name, {}, result) is None, name
    refused_dict = {"error": "x", "error_code": "execution_refused", "hint": DO_NOT_RESEND_HINT, "suggestion": "s"}
    assert toolkit.hint("execute_payout", {}, refused_dict) == DO_NOT_RESEND_HINT


async def test_simulation_trigger_hint_only_with_cached_simulated_capabilities(recording: RecordingTransport) -> None:
    toolkit = WhireToolkit(api_key="k", transport=httpx.MockTransport(_no_requests))
    draft = {**P.PAYOUT_DRAFT, "amount": 20.01}
    assert "triggers" not in (toolkit.hint("create_payout_draft", {}, draft) or "")  # capabilities unknown
    toolkit._capabilities = Capabilities.from_wire(P.fresh(P.CAPABILITIES_PRODUCTION))
    assert "triggers" not in (toolkit.hint("create_payout_draft", {}, draft) or "")
    toolkit._capabilities = Capabilities.from_wire(P.fresh(P.CAPABILITIES))
    hint = toolkit.hint("create_payout_draft", {}, draft)
    assert hint is not None and ".01" in hint and "insufficient funds" in hint
    assert "triggers" not in (toolkit.hint("create_payout_draft", {}, P.PAYOUT_DRAFT) or "")  # a plain amount
    recording.handler = answers("get_capabilities")
    cached = toolkit_for(recording)
    await cached.execute("get_capabilities", {})  # the toolkit caches the capabilities it fetched
    assert cached.capabilities is not None and cached.capabilities.simulated is True
    assert ".03" in (cached.hint("create_payout_draft", {}, {**P.PAYOUT_DRAFT, "amount": 20.03}) or "")


def test_system_prompt_states_the_verified_facts() -> None:
    prompt = WhireToolkit(api_key="k").system_prompt
    for needle in (
        "register_payer",
        "activate_payer_account",
        "authorize_payment",
        "submit_payout_for_review",
        "nextAction",
        "confirm_payout is optional",
        "execute_payout and pay_x402_resource move money",
        "unresolved",
        "never sent twice",
        "kyc_rejected and failed are terminal",
        "returned payout still consumes the mandate total",
        "camelCase",
        "at most two decimals",
        "every 2-3 seconds",
        "simulated: true",
        "Never pick an amount to test an outcome",
        "cumulative_limit",
        "1095",
        "ambiguous_outcome",
        "confirmation_required",
    ):
        assert needle in prompt, needle
    assert "webhook" in prompt.lower() and "/recipients" not in prompt and "/payments/send" not in prompt
