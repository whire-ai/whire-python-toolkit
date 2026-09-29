"""Secrets hygiene: reprs, exception text, agent dicts and logs never leak the API key or request-body data (SPEC §3)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from tests.conftest import RecordingTransport, fail, json_response, make_client, ok, text_response, tool_error
from tests.fixtures import payloads as P
from whire import AuthenticationError, NetworkError, ServerError, WhireClient, WhireError

SECRET_KEY = "sk-live-supersecret-9f8e7d6c5b4a"
PAYER_IBAN = "NL02RABO0123456789"
PAYER_EMAIL = "finance@merchant.example"
SENSITIVE = (SECRET_KEY, PAYER_IBAN, PAYER_EMAIL, "X-API-Key", "x-api-key", "Bearer ")


def assert_clean(text: str) -> None:
    for needle in SENSITIVE:
        assert needle not in text, f"{needle!r} leaked in {text!r}"


def assert_error_clean(error: WhireError) -> None:
    for text in (str(error), repr(error), json.dumps(error.to_agent_dict()), error.suggestion or ""):
        assert_clean(text)
    for name, value in vars(error).items():  # exceptions never hold httpx objects or the key
        assert not isinstance(value, (httpx.Request, httpx.Response, httpx.Headers, httpx.URL)) and value != SECRET_KEY, name


async def register_payer(client: WhireClient, **kwargs: Any) -> Any:
    """A POST whose body carries an IBAN and an email address."""
    kwargs.setdefault("idempotency_key", "hygiene-key-1")
    return await client.payers.create(
        legal_name="Merchant B.V.",
        contact_first_name="Eva",
        contact_last_name="Jansen",
        email=PAYER_EMAIL,
        phone="+31612345678",
        funding_sources=[{"type": "sepa", "destination": PAYER_IBAN, "holderName": "Merchant B.V."}],
        **kwargs,
    )


@pytest.fixture
def secret_client(recording: RecordingTransport) -> WhireClient:
    return make_client(recording, api_key=SECRET_KEY, max_retries=2, retry_base_delay=0.0, retry_max_delay=0.0)


def test_reprs_mask_the_key_and_hold_no_request_data(secret_client: WhireClient, recording: RecordingTransport) -> None:
    for text in (repr(secret_client), repr(secret_client.mcp), str(secret_client), repr(make_client(recording, api_key=SECRET_KEY, auth_scheme="bearer"))):
        assert_clean(text)
    assert "…" + SECRET_KEY[-4:] in repr(secret_client) and "…" + SECRET_KEY[-4:] in repr(secret_client.mcp)
    assert not any(value == SECRET_KEY for value in vars(secret_client).values()) and "api_key" not in vars(secret_client)


async def test_401_text_names_the_scheme_and_env_var_never_the_key(recording: RecordingTransport) -> None:
    for scheme, needle in (("x-api-key", "X-API-Key"), ("bearer", "Bearer")):
        recording.push(fail("Invalid API key", 401, {"x-railway-request-id": "req-1"}))
        async with make_client(recording, api_key=SECRET_KEY, auth_scheme=scheme) as client:  # type: ignore[arg-type]
            with pytest.raises(AuthenticationError) as info:
                await register_payer(client)
        error = info.value
        assert needle in str(error) and "WHIRE_API_KEY" in str(error) and error.needs_user_action  # the SCHEME is named on purpose
        for text in (str(error), repr(error), json.dumps(error.to_agent_dict())):
            assert SECRET_KEY not in text and PAYER_IBAN not in text and PAYER_EMAIL not in text
        for value in vars(error).values():
            assert not isinstance(value, (httpx.Request, httpx.Response, httpx.Headers)) and value != SECRET_KEY
    recording.push(json_response({"error": "Invalid API key"}, 401))
    async with make_client(recording, api_key=SECRET_KEY) as client:
        with pytest.raises(AuthenticationError) as mcp_info:
            await client.mcp.list_tools()
    assert "WHIRE_API_KEY" in str(mcp_info.value) and SECRET_KEY not in str(mcp_info.value) and SECRET_KEY not in repr(mcp_info.value)


async def test_error_text_after_retries_and_network_failures_is_clean(secret_client: WhireClient, recording: RecordingTransport, no_sleep: list[float]) -> None:
    async with secret_client as client:
        recording.push(*(fail("Internal error", 500) for _ in range(3)))
        with pytest.raises(ServerError) as server:
            await register_payer(client)
        assert_error_clean(server.value)
        assert server.value.idempotency_key == "hygiene-key-1"  # the key that IS meant to be surfaced
        recording.push(*(httpx.ConnectError("connection refused") for _ in range(3)))
        with pytest.raises(NetworkError) as network:
            await register_payer(client)
        assert_error_clean(network.value)
        assert "ConnectError" in str(network.value)
        recording.push(text_response("error code: 1010", 403))
        with pytest.raises(WhireError) as text:
            await register_payer(client)
        assert_error_clean(text.value)
        recording.push(*(fail("in flight", 409) for _ in range(3)))
        with pytest.raises(WhireError) as conflict:
            await register_payer(client)
        assert conflict.value.error_code == "idempotency_in_flight" and "hygiene-key-1" in (conflict.value.suggestion or "")
        assert_error_clean(conflict.value)
        recording.push(lambda request: tool_error(f"MCP error -32602: Input validation error: iban {PAYER_IBAN} is invalid", 2))
        with pytest.raises(WhireError) as tool_info:
            await client.mcp.call_tool("validate_iban", {"iban": PAYER_IBAN})
        for value in (str(tool_info.value), repr(tool_info.value), json.dumps(tool_info.value.to_agent_dict())):
            assert SECRET_KEY not in value and "x-api-key" not in value.lower()  # the tool's own sentence may quote the argument
    recording.push(httpx.ReadTimeout("slow read"))
    async with make_client(recording, api_key=SECRET_KEY, max_retries=3, auto_idempotency=False) as client:
        with pytest.raises(WhireError) as ambiguous:
            await register_payer(client, idempotency_key=None)
    assert ambiguous.value.error_code == "ambiguous_outcome"
    assert_error_clean(ambiguous.value)


async def test_whire_logger_never_logs_headers_bodies_query_strings_or_server_sentences(
    secret_client: WhireClient, recording: RecordingTransport, no_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    sentence = f"Funding source {PAYER_IBAN} already belongs to another payer."
    recording.push(fail("Internal error", 500), httpx.ReadTimeout("slow"), ok(P.PAYER_PENDING))
    recording.push(ok(P.PAYOUT_LIST_PAID, headers={"x-railway-request-id": "req-77"}), fail(sentence, 400, {"x-railway-request-id": "req-400"}))
    with caplog.at_level(logging.DEBUG):
        async with secret_client as client:
            payer = await register_payer(client)
            await client.payouts.list(status="paid")
            with pytest.raises(WhireError) as info:
                await register_payer(client)
    assert str(info.value) == sentence  # the caller needs the server's reason ...
    assert SECRET_KEY not in repr(payer) and SECRET_KEY not in json.dumps(payer.to_dict())
    whire_records = [r for r in caplog.records if r.name == "whire" or r.name.startswith("whire.")]
    assert whire_records, "the whire logger should emit DEBUG lines per attempt"
    for record in caplog.records:  # every logger, including httpx
        assert_clean(record.getMessage())
        assert_clean(str(record.args))
        assert "Merchant B.V." not in record.getMessage() and sentence not in record.getMessage()  # ... but the logs never carry it
    attempts = [r.getMessage() for r in whire_records if "POST /api/payers" in r.getMessage()]
    assert len(attempts) == 4 and "status=500 attempt=1" in attempts[0] and "ReadTimeout" in attempts[1] and "status=200 attempt=3" in attempts[2]
    assert all("idempotency_key=hygiene-key-1" in line and "elapsed_ms=" in line for line in attempts)
    assert "status=400" in attempts[3] and "request_id=req-400" in attempts[3]
    lines = [r.getMessage() for r in whire_records if "GET /api/payouts" in r.getMessage()]
    assert lines and "request_id=req-77" in lines[0] and "status=paid" not in lines[0]  # no query strings
    caplog.clear()
    recording.push(ok(P.HEALTH))
    with caplog.at_level(logging.INFO, logger="whire"):
        async with make_client(recording, api_key=None) as anonymous:
            await anonymous.health()
    messages = [r.getMessage() for r in caplog.records if r.name == "whire"]
    assert any("using base URL https://sandbox.whire.ai" in m for m in messages) and any("no API key configured" in m for m in messages)
    for message in messages:
        assert_clean(message)
