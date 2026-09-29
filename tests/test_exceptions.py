"""Error hierarchy: codes, statuses, flags, agent dicts, repr/str, and the REST/MCP not-found mapping (SPEC §6)."""

from __future__ import annotations

import json

import pytest

import whire
from tests.conftest import RecordingTransport, fail, rpc_reply_for, tool_failure
from tests.fixtures import payloads as P
from whire import (
    AmbiguousResponseError,
    AuthenticationError,
    BadRequestError,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidInputError,
    MCPProtocolError,
    NetworkError,
    NotFoundError,
    PayoutExecutionRefused,
    RateLimitError,
    ResponseFormatError,
    ServerError,
    ToolError,
    WhireClient,
    WhireError,
    WhireTimeoutError,
)
from whire import exceptions as exceptions_module

AGENT_DICT_KEYS = {"error", "error_code", "status_code", "retryable", "needs_user_action", "is_input_error", "suggestion", "request_id", "idempotency_key"}

# class, default error_code, default status, retryable, needs_user_action, is_input_error
TABLE: list[tuple[type[WhireError], str | None, int | None, bool, bool, bool]] = [
    (WhireError, None, None, False, False, False), (AuthenticationError, "auth_failed", 401, False, True, False),
    (NotFoundError, "not_found", 400, False, False, True), (BadRequestError, "bad_request", 400, False, False, True),
    (PayoutExecutionRefused, "execution_refused", 400, False, True, False), (IdempotencyConflictError, "idempotency_in_flight", 409, True, False, False),
    (IdempotencyMismatchError, "idempotency_mismatch", 422, False, False, True), (RateLimitError, "rate_limited", 429, True, False, False),
    (ServerError, "server_error", 500, True, False, False), (NetworkError, "network_error", None, True, False, False),
    (AmbiguousResponseError, "ambiguous_outcome", None, False, True, False), (ResponseFormatError, "invalid_response", None, False, False, False),
    (InvalidInputError, "invalid_input", None, False, False, True), (ToolError, "tool_error", None, False, False, True),
    (MCPProtocolError, "mcp_error", None, False, False, False), (WhireTimeoutError, "timeout", None, False, False, False),
]


def test_defaults_agent_dict_and_repr_per_class() -> None:
    assert {row[0] for row in TABLE} == {getattr(exceptions_module, n) for n in exceptions_module.__all__}
    assert all(getattr(whire, n) is getattr(exceptions_module, n) for n in exceptions_module.__all__)
    for cls, code, status, retryable, user_action, input_error in TABLE:
        error = cls("boom", request_id="req-1", idempotency_key="key-1")
        assert (error.error_code, error.status_code) == (code, status), cls
        assert (error.is_retryable, error.needs_user_action, error.is_input_error) == (retryable, user_action, input_error), cls
        assert str(error) == error.message == "boom" and "req-1" not in str(error)
        payload = error.to_agent_dict()
        assert set(payload) == AGENT_DICT_KEYS and json.dumps(payload)
        assert (payload["error"], payload["error_code"], payload["status_code"]) == ("boom", code, status)
        assert (payload["retryable"], payload["needs_user_action"], payload["is_input_error"]) == (retryable, user_action, input_error)
        assert payload["request_id"] == "req-1" and payload["idempotency_key"] == "key-1"
        assert repr(error).startswith(cls.__name__ + "(") and "req-1" in repr(error) and "key-1" in repr(error)


def test_constructor_overrides_and_class_specific_fields() -> None:
    assert NotFoundError("x", status_code=None).status_code is None and NotFoundError("x", status_code=404).status_code == 404
    custom = BadRequestError("x", error_code="custom", suggestion="do this")
    assert custom.error_code == "custom" and custom.to_agent_dict()["suggestion"] == "do this"
    assert WhireError("x").error_code is None and WhireError("x").suggestion is None
    limited = RateLimitError("slow down", retry_after=12.5)
    assert limited.retry_after == 12.5 and "retry_after=12.5" in repr(limited) and RateLimitError("x").retry_after is None
    assert ResponseFormatError("bad", payload={"weird": 1}).payload == {"weird": 1}
    assert "payload" not in ResponseFormatError("bad", payload={"weird": 1}).to_agent_dict()
    tool = ToolError("msg", tool_name="get_payer", data={"issues": []}, error_code="unknown_tool")
    assert tool.tool_name == "get_payer" and tool.data == {"issues": []} and "tools/list" in (tool.suggestion or "")
    assert "tool_name='get_payer'" in repr(tool)
    protocol = MCPProtocolError("Method not found", code=-32601, error_code="method_not_found")
    assert protocol.code == -32601 and "code=-32601" in repr(protocol) and not protocol.is_input_error
    assert MCPProtocolError("Payout nope not found.", code=-32603, input_error=True).is_input_error
    timeout = WhireTimeoutError("timed out", payout_id="p-1", last="payout", elapsed=61.2)
    assert isinstance(timeout, TimeoutError) and (timeout.payout_id, timeout.last, timeout.elapsed) == ("p-1", "payout", 61.2)
    assert "nothing was sent" in (InvalidInputError("amount must be greater than zero").suggestion or "")


def test_money_safety_suggestions() -> None:
    refused = PayoutExecutionRefused(P.ERROR_INSUFFICIENT_FUNDS)
    assert isinstance(refused, BadRequestError) and refused.error_code == "execution_refused"
    assert refused.needs_user_action and not refused.is_input_error and not refused.is_retryable
    assert all(needle in (refused.suggestion or "") for needle in ("Do not resend", "get_payout_status", "human"))
    assert PayoutExecutionRefused(P.ERROR_ALREADY_EXECUTED).error_code == "already_executed"
    assert PayoutExecutionRefused(P.ERROR_ALREADY_EXECUTED, error_code="execution_refused").error_code == "execution_refused"
    conflict = IdempotencyConflictError("in flight", idempotency_key="abc-123")
    assert "idempotency_key='abc-123'" in (conflict.suggestion or "") and "do not create a new one" in (conflict.suggestion or "")
    mismatch = IdempotencyMismatchError(P.ERROR_IDEMPOTENCY_MISMATCH)
    assert "new" in (mismatch.suggestion or "").lower() and "key" in (mismatch.suggestion or "").lower()
    ambiguous = AmbiguousResponseError("read timeout")
    assert isinstance(ambiguous, NetworkError) and "may have been processed" in (ambiguous.suggestion or "")
    route = NotFoundError(P.ERROR_ROUTE_NOT_FOUND, status_code=404, error_code="route_not_found")
    assert "base_url" in (route.suggestion or "") and "SDK version" in (route.suggestion or "")


# --------------------------------------------------------------------------- mapping via REST and via MCP-backed namespaces


async def test_not_found_and_bad_request_via_rest_and_mcp(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(fail(P.ERROR_PAYER_NOT_FOUND, 400, {"x-railway-request-id": "req-9"}))
    with pytest.raises(NotFoundError) as rest:
        await client.payers.get("nope")
    assert rest.value.status_code == 400 and rest.value.error_code == "not_found" and rest.value.request_id == "req-9"
    for text, cls, code in (
        (P.ERROR_BENEFICIARY_NOT_FOUND, NotFoundError, "not_found"),
        ("User nope Not Found", NotFoundError, "not_found"),  # case-insensitive, trailing period optional
        (P.MCP_ERROR_INVALID_ARGUMENTS, BadRequestError, "invalid_arguments"),
        (P.MCP_ERROR_ARGUMENTS_MISSING, BadRequestError, "invalid_arguments"),
        (P.MCP_ERROR_UNKNOWN_TOOL, BadRequestError, "invalid_arguments"),  # "Tool nope not found" is not a missing record
        (P.ERROR_EVENT_REFUSED, BadRequestError, "bad_request"),
    ):
        recording.push(tool_failure(text))
        with pytest.raises(cls) as info:
            await client.beneficiaries.get("nope")
        error = info.value
        assert type(error) is cls and error.error_code == code and error.status_code is None and str(error) == text, text
        assert error.is_input_error and not isinstance(error, (ToolError, MCPProtocolError))  # MCP errors never escape a namespace
    for error, cls, code in (
        ({"code": -32603, "message": "User nope not found."}, NotFoundError, "not_found"),
        (P.MCP_JSONRPC_UNKNOWN_SCHEME, BadRequestError, "invalid_arguments"),
        (P.MCP_JSONRPC_METHOD_NOT_FOUND, WhireError, "mcp_error"),  # a protocol failure, not a missing record
    ):
        recording.push(lambda request, error=error: rpc_reply_for(request, error=error))
        with pytest.raises(cls) as info:
            await client.users.get("nope")
        assert type(info.value) is cls and info.value.error_code == code, error
        assert info.value.status_code == (200 if cls is WhireError else None)
