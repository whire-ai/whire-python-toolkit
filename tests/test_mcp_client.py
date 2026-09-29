"""``WhireMCPClient``: JSON-RPC 2.0 over ``POST /mcp`` (SPEC §1 MCP facts, §4)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import (
    DEFAULT_API_KEY,
    RETRY_KW,
    RecordingTransport,
    json_response,
    make_client,
    make_mcp_client,
    rpc_error,
    rpc_reply_for,
    rpc_request,
    rpc_result,
    text_response,
    tool_answer,
    tool_failure,
    tool_reply_for,
)
from tests.fixtures import payloads as P
from whire import AmbiguousResponseError, AuthenticationError, MCPProtocolError, NetworkError, ResponseFormatError, ToolError, WhireError, WhireMCPClient, __version__
from whire.mcp_client import KNOWN_PROTOCOL_VERSIONS, MUTATING_TOOLS, PROPOSED_PROTOCOL_VERSION, READ_ONLY_TOOLS
from whire.models import Completion, PromptMessage, ResourceContents, ServerInfo, ToolDefinition

TOOLS_LIST_RESULT: dict[str, Any] = json.loads((Path(__file__).parent / "fixtures" / "tools_list.json").read_text())["result"]


@pytest.fixture
def mcp(recording: RecordingTransport) -> WhireMCPClient:
    """A standalone ``WhireMCPClient`` on a fresh recording transport (``max_retries=0``)."""
    return make_mcp_client(recording)


def echo(result: Any):
    """An answer callable that echoes the request id around ``result``."""
    return lambda request: rpc_reply_for(request, result)


# --------------------------------------------------------------------------- wire shape


async def test_request_headers_endpoint_auth_and_ids(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    recording.handler = echo({"tools": []})
    await mcp.ping()
    sent = recording.last
    assert sent.method == "POST" and sent.url == "https://sandbox.whire.ai/mcp" and sent.idempotency_key
    assert sent.headers["content-type"] == "application/json" and sent.headers["accept"] == "application/json, text/event-stream"
    assert sent.headers["x-api-key"] == DEFAULT_API_KEY and sent.headers["user-agent"].startswith(f"whire-python/{__version__} httpx/")
    assert "mcp-protocol-version" not in sent.headers
    assert DEFAULT_API_KEY not in repr(mcp) and "…abcd" in repr(mcp) and mcp.endpoint == "https://sandbox.whire.ai/mcp"
    await mcp.list_tools()
    await mcp.ping()
    messages = [r.json for r in recording.requests]
    assert [m["id"] for m in messages] == [1, 2, 3] and [m["method"] for m in messages] == ["ping", "tools/list", "ping"]
    assert all(m["jsonrpc"] == "2.0" and isinstance(m["params"], dict) for m in messages)
    recording.push(tool_answer(P.PAYER_ACTIVE))
    await mcp.call_tool("get_payer", {"payerId": "é"})
    assert recording.last.body == '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_payer","arguments":{"payerId":"é"}}}'.encode()
    await make_mcp_client(recording, auth_scheme="bearer").ping()
    assert recording.last.headers["authorization"] == f"Bearer {DEFAULT_API_KEY}" and "x-api-key" not in recording.last.headers
    await make_mcp_client(recording, api_key=None, auto_idempotency=False).ping()
    assert "x-api-key" not in recording.last.headers and recording.last.idempotency_key is None
    full = WhireMCPClient("https://example.test/mcp/", transport=httpx.MockTransport(lambda r: rpc_result({})))
    assert full.base_url == "https://example.test" and full.endpoint == "https://example.test/mcp"
    await full.close()


# --------------------------------------------------------------------------- tools/call


async def test_call_tool_result_handling(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    recording.handler = tool_answer(P.PAYERS_LIST)
    for arguments in ((), (None,), ({},)):
        await mcp.call_tool("list_payers", *arguments)
        assert recording.last.tool_call == ("list_payers", {})  # an arguments object is ALWAYS sent
    recording.push(tool_answer(P.PAYER_ACTIVE))
    assert await mcp.call_tool("get_payer", {"payerId": P.PAYER_ID}) == P.PAYER_ACTIVE
    recording.push(echo({"content": [{"type": "text", "text": json.dumps(P.PAYER_ACTIVE, indent=2)}]}))
    assert await mcp.call_tool("get_payer", {"payerId": P.PAYER_ID}) == P.PAYER_ACTIVE  # falls back to the text JSON
    recording.push(echo({"content": [{"type": "text", "text": "plain words"}]}))
    assert await mcp.call_tool("get_payer", {"payerId": P.PAYER_ID}) == {"text": "plain words"}
    recording.push(echo({"content": [{"type": "text", "text": json.dumps({"stale": True})}], "structuredContent": P.CAPABILITIES}))
    assert await mcp.call_tool("get_capabilities") == P.CAPABILITIES  # structuredContent wins
    recording.push(echo(["not", "an", "object"]))
    with pytest.raises(ResponseFormatError):
        await mcp.call_tool("get_capabilities")


async def test_call_tool_is_error_families(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    for text, code, name, data_check in (
        (P.MCP_ERROR_UNKNOWN_TOOL, "unknown_tool", "nope", lambda d: d is None),
        (P.MCP_ERROR_INVALID_ARGUMENTS, "invalid_arguments", "get_payer", lambda d: isinstance(d, str) and "invalid_type" in d),
        (P.MCP_ERROR_ARGUMENTS_MISSING, "invalid_arguments", "list_payers", lambda d: isinstance(d, str)),
        (P.MCP_ERROR_BUSINESS_NOT_FOUND, "tool_error", "get_payer", lambda d: d is None),
        (P.MCP_ERROR_BUSINESS_EXECUTED, "tool_error", "execute_payout", lambda d: d is None),
    ):
        recording.push(tool_failure(text))
        with pytest.raises(ToolError) as info:
            await mcp.call_tool(name, {})
        error = info.value
        assert error.error_code == code and error.tool_name == name and str(error) == text and data_check(error.data), text
        assert error.is_input_error and not error.is_retryable and error.status_code is None
        assert DEFAULT_API_KEY not in repr(error) and error.to_agent_dict()["error_code"] == code
    recording.push(echo({"content": [], "isError": True}))
    with pytest.raises(ToolError) as info:
        await mcp.call_tool("get_payer", {"payerId": "x"})
    assert info.value.error_code == "tool_error" and "get_payer" in str(info.value)


# --------------------------------------------------------------------------- JSON-RPC and HTTP errors


async def test_jsonrpc_errors_on_200(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    for error, code, input_error, method in (
        (P.MCP_JSONRPC_METHOD_NOT_FOUND, "method_not_found", False, lambda: mcp.request("tools/nope")),
        (P.MCP_JSONRPC_RESOURCE_MISSING, "mcp_error", True, lambda: mcp.read_resource("payout://nope")),
        (P.MCP_JSONRPC_UNKNOWN_SCHEME, "mcp_error", True, lambda: mcp.read_resource("nope://x")),
        ({"code": -32603, "message": "Internal error", "data": {"x": 1}}, "mcp_error", False, lambda: mcp.ping()),
    ):
        recording.push(lambda request, error=error: rpc_reply_for(request, error=error))
        with pytest.raises(MCPProtocolError) as info:
            await method()
        assert info.value.code == error["code"] and info.value.error_code == code and str(info.value) == error["message"], error
        assert info.value.is_input_error is input_error and info.value.data == error.get("data") and not info.value.is_retryable
    recording.push(json_response({"ok": True, "data": {}}), text_response("<html>", 200, "text/html"))
    for _ in range(2):
        with pytest.raises(ResponseFormatError):
            await mcp.ping()


async def test_http_errors_with_jsonrpc_bodies_and_hints(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    recording.push(json_response(P.MCP_JSONRPC_PARSE_ERROR, 400, {"x-railway-request-id": "req-123"}))
    with pytest.raises(MCPProtocolError) as info:
        await mcp.ping()
    assert info.value.code == -32700 and info.value.status_code == 400 and info.value.request_id == "req-123"
    assert str(info.value) == "Parse error: Invalid JSON" and not info.value.is_retryable
    recording.push(rpc_error(-32000, P.MCP_JSONRPC_NOT_ACCEPTABLE["message"], status=406))
    with pytest.raises(MCPProtocolError) as info:
        await mcp.ping()
    assert info.value.status_code == 406 and "Accept: application/json, text/event-stream" in str(info.value)
    recording.push(rpc_error(-32000, "Method not allowed", status=405))
    with pytest.raises(MCPProtocolError) as info:
        await mcp.ping()
    assert info.value.status_code == 405 and "POST" in str(info.value)
    recording.push(text_response("error code: 1010", 403), json_response({"ok": False, "error": "Not found."}, 404))
    with pytest.raises(WhireError) as info:
        await mcp.ping()
    assert type(info.value) is WhireError and info.value.status_code == 403 and str(info.value) == "error code: 1010"
    with pytest.raises(WhireError) as info:
        await mcp.ping()
    assert info.value.status_code == 404 and str(info.value) == "Not found."
    for body in ({"error": "Invalid API key"}, {"ok": False, "error": "Invalid API key"}, P.MCP_JSONRPC_PARSE_ERROR):
        recording.push(json_response(body, 401))
        with pytest.raises(AuthenticationError) as auth:
            await mcp.ping()
        assert auth.value.status_code == 401 and "X-API-Key" in str(auth.value) and DEFAULT_API_KEY not in str(auth.value)
    recording.push(json_response({"error": "nope"}, 401), text_response("Unauthorized", 401))
    with pytest.raises(AuthenticationError) as bearer:
        await make_mcp_client(recording, auth_scheme="bearer").ping()
    assert "Authorization: Bearer" in str(bearer.value)
    with pytest.raises(AuthenticationError) as anonymous:
        await make_mcp_client(recording, api_key=None).ping()
    assert "no API key was sent" in str(anonymous.value)


# --------------------------------------------------------------------------- notifications and initialize


async def test_notify(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    recording.push(httpx.Response(202), httpx.Response(204), echo({}))
    assert await mcp.notify("notifications/initialized") is None
    assert recording.last.json == {"jsonrpc": "2.0", "method": "notifications/initialized"}  # no id
    await mcp.notify("notifications/cancelled", {"requestId": 7})
    assert recording.last.json["params"] == {"requestId": 7}
    await mcp.ping()
    assert recording.last.json["id"] == 1  # notifications do not consume request ids
    recording.push(rpc_error(-32600, "Invalid Request", None, status=200), json_response({"error": "nope"}, 401))
    with pytest.raises(MCPProtocolError) as info:
        await mcp.notify("notifications/initialized")
    assert info.value.code == -32600
    with pytest.raises(AuthenticationError):
        await mcp.notify("notifications/initialized")


async def test_initialize_negotiates_caches_and_sends_the_protocol_header(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    recording.push(echo(P.MCP_INITIALIZE_RESULT), httpx.Response(202), echo({}))
    assert mcp.protocol_version is None and mcp.server_info is None
    results = await asyncio.gather(mcp.initialize(), mcp.initialize(), mcp.initialize())  # concurrency-safe, one handshake
    info = results[0]
    assert isinstance(info, ServerInfo) and len({id(r) for r in results}) == 1 and len(recording) == 2
    assert info.name == "agent-payouts-mcp" and info.protocol_version == "2025-06-18" and info.capabilities["completions"] == {}
    assert mcp.protocol_version == "2025-06-18" and mcp.server_info is info and await mcp.initialize() is info
    init_request, initialized = recording.requests
    assert init_request.json["method"] == "initialize" and init_request.json["params"]["protocolVersion"] == PROPOSED_PROTOCOL_VERSION == "2025-06-18"
    assert init_request.json["params"]["clientInfo"] == {"name": "whire-python", "version": __version__}
    assert "mcp-protocol-version" not in init_request.headers and initialized.json == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    await mcp.ping()
    assert recording.last.headers["mcp-protocol-version"] == "2025-06-18" and recording.last.json["id"] == 2
    for version in (*sorted(KNOWN_PROTOCOL_VERSIONS), "2099-01-01"):  # any negotiated value is echoed back
        other = make_mcp_client(recording, client_info=("my-agent", "9.9"))
        recording.push(echo({**P.MCP_INITIALIZE_RESULT, "protocolVersion": version}), httpx.Response(202), echo({}))
        assert (await other.initialize()).protocol_version == version
        await other.ping()
        assert recording.last.headers["mcp-protocol-version"] == version
    assert recording.requests[-3].json["params"]["clientInfo"] == {"name": "my-agent", "version": "9.9"}
    failed = make_mcp_client(recording)
    recording.push(rpc_error(-32000, "Unsupported protocol version", status=400), echo({}), echo("nonsense"))
    with pytest.raises(MCPProtocolError):
        await failed.initialize()
    await failed.ping()
    assert failed.protocol_version is None and "mcp-protocol-version" not in recording.last.headers  # a failed handshake leaves no header
    with pytest.raises(ResponseFormatError):
        await failed.initialize()


# --------------------------------------------------------------------------- tools/list, resources, prompts, completion


async def test_list_tools_metadata(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    recording.push(echo(TOOLS_LIST_RESULT))
    tools = await mcp.list_tools()
    assert recording.last.json["method"] == "tools/list" and recording.last.json["params"] == {}
    assert len(tools) == 33 and all(isinstance(tool, ToolDefinition) for tool in tools)
    by_name = {tool.name: tool for tool in tools}
    execute = by_name["execute_payout"]
    assert execute.title and execute.description and execute.input_schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert execute.output_schema is not None and execute.output_schema["additionalProperties"] is False
    assert execute.annotations == {"destructiveHint": True, "idempotentHint": False, "openWorldHint": True} and execute.destructive
    assert {tool.name for tool in tools if tool.destructive} == {"execute_payout", "pay_x402_resource"}
    assert by_name["list_payers"].annotations is None and all(tool.execution == {"taskSupport": "forbidden"} for tool in tools)
    assert execute.raw == next(t for t in TOOLS_LIST_RESULT["tools"] if t["name"] == "execute_payout")
    assert READ_ONLY_TOOLS | MUTATING_TOOLS == set(by_name) and not READ_ONLY_TOOLS & MUTATING_TOOLS
    recording.push(echo({"tools": [{"name": "ping_tool"}, "junk"]}), echo({"nope": []}))
    minimal = await mcp.list_tools()
    assert [t.name for t in minimal] == ["ping_tool"] and minimal[0].input_schema == {} and minimal[0].output_schema is None
    with pytest.raises(ResponseFormatError):
        await mcp.list_tools()


async def test_resources_prompts_completion_ping_and_raw_request(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    recording.push(echo(P.MCP_RESOURCES_LIST_RESULT), echo(P.MCP_TEMPLATES_LIST_RESULT), echo(P.MCP_RESOURCE_READ_RESULT))
    resources = await mcp.list_resources()
    assert recording.last.json["method"] == "resources/list" and [r.uri for r in resources] == ["config://provider", "policy://limits"]
    assert resources[0].title == "Deployment capabilities" and resources[0].mime_type is None
    templates = await mcp.list_resource_templates()
    assert recording.last.json["method"] == "resources/templates/list" and [t.uri_template for t in templates] == ["payout://{payoutId}", "mandate://{mandateId}"]
    contents = await mcp.read_resource("config://provider")
    assert recording.last.json["method"] == "resources/read" and recording.last.json["params"] == {"uri": "config://provider"}
    assert isinstance(contents, ResourceContents) and contents.contents[0].media_type == "application/json"  # the server's ``mediaType``
    assert contents.json == P.PROVIDER_RESOURCE_JSON and contents.text == contents.contents[0].text and contents.raw == P.MCP_RESOURCE_READ_RESULT
    recording.push(echo({"contents": [{"uri": "policy://limits", "mimeType": "application/json", "text": '{"maxDurationDays": 1095}'}]}))
    standard = await mcp.read_resource("policy://limits")
    assert standard.contents[0].media_type == "application/json" and standard.json == {"maxDurationDays": 1095}
    recording.push(echo({"contents": [{"uri": "x://1", "blob": "AAAA", "mediaType": "application/octet-stream"}, {"uri": "x://3", "text": "[1]"}]}))
    mixed = await mcp.read_resource("x://1")
    assert mixed.contents[0].blob == "AAAA" and mixed.json == [1]  # the first JSON text wins
    recording.push(echo(P.MCP_PROMPTS_LIST_RESULT), echo(P.MCP_PROMPT_GET_RESULT), echo(P.MCP_PROMPT_GET_RESULT))
    prompts = await mcp.list_prompts()
    assert recording.last.json["method"] == "prompts/list" and [p.name for p in prompts] == ["run_agent_payout_flow", "run_payment_mandate_flow"]
    assert prompts[0].title == "Run agent payout flow" and prompts[0].arguments == []
    messages = await mcp.get_prompt("run_agent_payout_flow")
    assert recording.last.json["params"] == {"name": "run_agent_payout_flow"} and isinstance(messages[0], PromptMessage)
    assert messages[0].role == "user" and (messages[0].text or "").startswith("Follow this sequence")
    await mcp.get_prompt("run_agent_payout_flow", {"payoutId": P.PAYOUT_ID})
    assert recording.last.json["params"] == {"name": "run_agent_payout_flow", "arguments": {"payoutId": P.PAYOUT_ID}}
    recording.push(echo(P.MCP_COMPLETION_RESULT), echo({}), echo({}), echo({"anything": [1, 2]}))
    ref, argument = {"type": "ref/resource", "uri": "payout://{payoutId}"}, {"name": "payoutId", "value": "702c"}
    completion = await mcp.complete(ref, argument)
    assert recording.last.json["method"] == "completion/complete" and recording.last.json["params"] == {"ref": ref, "argument": argument}
    assert isinstance(completion, Completion) and completion.values == [P.PAYOUT_ID] and completion.total == 1
    empty = await mcp.complete({"type": "ref/prompt", "name": "p"}, {"name": "x", "value": ""})
    assert empty.values == [] and empty.total is None and empty.has_more is False
    assert await mcp.ping() is None and recording.last.json["method"] == "ping" and recording.last.json["params"] == {}
    assert await mcp.request("custom/method", {"a": 1}) == {"anything": [1, 2]} and recording.last.json["params"] == {"a": 1}


# --------------------------------------------------------------------------- retry policy (money safety)


async def test_read_only_tools_and_protocol_methods_are_retried(no_sleep: list[float]) -> None:
    recording = RecordingTransport().push(httpx.ReadTimeout("slow"), tool_answer(P.PAYER_ACTIVE))
    assert await make_mcp_client(recording, **RETRY_KW).call_tool("get_payer", {"payerId": P.PAYER_ID}) == P.PAYER_ACTIVE
    assert len(recording) == 2 and recording.requests[0].body == recording.requests[1].body
    assert recording.requests[0].idempotency_key == recording.requests[1].idempotency_key and len(no_sleep) == 1
    for status in (500, 502, 503, 504, 429, 409):
        recording = RecordingTransport().push(json_response({"error": "later"}, status), tool_answer(P.CAPABILITIES))
        assert await make_mcp_client(recording, **RETRY_KW).call_tool("get_capabilities") == P.CAPABILITIES and len(recording) == 2, status
    for name in sorted(READ_ONLY_TOOLS):
        recording = RecordingTransport().push(httpx.ReadTimeout("slow"), tool_answer({"ok": 1}))
        assert await make_mcp_client(recording, **RETRY_KW).call_tool(name, {}) == {"ok": 1} and len(recording) == 2, name
    results = {
        "tools/list": {"tools": []}, "resources/list": {"resources": []}, "resources/templates/list": {"resourceTemplates": []},
        "resources/read": {"contents": []}, "prompts/list": {"prompts": []}, "prompts/get": {"messages": []},
        "completion/complete": {"completion": {"values": []}}, "ping": {}, "initialize": P.MCP_INITIALIZE_RESULT,
    }
    recording = RecordingTransport()
    recording.handler = lambda request: rpc_reply_for(request, results.get(rpc_request(request)["method"], {}))
    mcp = make_mcp_client(recording, **RETRY_KW)
    for call in (
        mcp.list_tools, mcp.list_resources, mcp.list_resource_templates, lambda: mcp.read_resource("config://provider"), mcp.list_prompts,
        lambda: mcp.get_prompt("run_agent_payout_flow"), lambda: mcp.complete({"type": "ref/prompt", "name": "p"}, {"name": "a", "value": ""}),
        mcp.ping, mcp.initialize,
    ):
        before = len(recording)
        recording.push(httpx.ReadTimeout("slow"), json_response({"error": "x"}, 500))
        await call()
        attempts = recording.requests[before : before + 3]
        assert [r.json.get("method") for r in attempts] == [attempts[0].json["method"]] * 3
    recording.push(httpx.ReadTimeout("slow"), tool_answer(P.CAPABILITIES))
    before = len(recording)
    await mcp.request("tools/call", {"name": "get_capabilities", "arguments": {}})  # the raw escape hatch follows the tool policy
    assert len(recording) == before + 2


async def test_mutating_tools_are_ambiguous_after_reaching_the_server(no_sleep: list[float]) -> None:
    for name in (*sorted(MUTATING_TOOLS), "some_future_tool"):  # unknown names are treated as mutating
        recording = RecordingTransport().push(httpx.ReadTimeout("slow"))
        with pytest.raises(AmbiguousResponseError):
            await make_mcp_client(recording, **RETRY_KW).call_tool(name, {})
        assert len(recording) == 1, name
    answers: list[Any] = [httpx.ReadError("eof"), httpx.WriteError("eof"), httpx.WriteTimeout("slow"), httpx.RemoteProtocolError("bad")]
    answers += [json_response({"error": "boom"}, status) for status in (500, 502, 504)]
    for answer in answers:
        recording = RecordingTransport().push(answer)
        with pytest.raises(AmbiguousResponseError) as info:
            await make_mcp_client(recording, **RETRY_KW).call_tool("create_beneficiary", {"fullName": "x"})
        assert len(recording) == 1 and not info.value.is_retryable and info.value.needs_user_action, answer
    recording = RecordingTransport().push(httpx.ReadTimeout("slow"))
    with pytest.raises(AmbiguousResponseError):
        await make_mcp_client(recording, **RETRY_KW).request("tools/call", {"name": "execute_payout", "arguments": {}})
    assert no_sleep == []
    answers = [json_response({"error": "later"}, 429), json_response({"error": "later"}, 503)]
    answers += [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")]
    for answer in answers:  # retried only when the request never reached the handler
        recording = RecordingTransport().push(answer, tool_answer(P.BENEFICIARY))
        assert await make_mcp_client(recording, **RETRY_KW).call_tool("create_beneficiary", {}) == P.BENEFICIARY and len(recording) == 2, answer
    assert len(no_sleep) == len(answers)
    recording = RecordingTransport().push(httpx.ConnectError("refused"))
    with pytest.raises(NetworkError) as info:
        await make_mcp_client(recording).call_tool("register_payer", {})  # max_retries=0
    assert not isinstance(info.value, AmbiguousResponseError) and info.value.is_retryable
    for status in (400, 401, 404, 406, 422):  # 4xx never retried
        recording = RecordingTransport().push(json_response({"jsonrpc": "2.0", "error": {"code": -32000, "message": "no"}, "id": None}, status))
        with pytest.raises(WhireError):
            await make_mcp_client(recording, **RETRY_KW).call_tool("get_capabilities")
        assert len(recording) == 1, status


async def test_long_running_tools_use_the_execute_timeout() -> None:
    seen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        name = rpc_request(request)["params"]["name"]
        seen[name] = request.extensions["timeout"]["read"]
        return tool_reply_for(request, {})

    mcp = make_mcp_client(handler, execute_timeout=150.0, timeout=20.0)
    for name in ("execute_payout", "pay_x402_resource", "get_payer", "create_beneficiary"):
        await mcp.call_tool(name, {})
    await mcp.close()
    assert seen == {"execute_payout": 150.0, "pay_x402_resource": 150.0, "get_payer": 20.0, "create_beneficiary": 20.0}
    seen.clear()
    async with make_client(handler, execute_timeout=61.0, timeout=13.0) as shared:  # via WhireClient.mcp too
        await shared.mcp.call_tool("execute_payout", {"payoutId": P.PAYOUT_ID})
        await shared.mcp.call_tool("get_payout_status", {"payoutId": P.PAYOUT_ID})
    assert seen == {"execute_payout": 61.0, "get_payout_status": 13.0}


# --------------------------------------------------------------------------- SSE, lifecycle, sharing with WhireClient


async def test_defensive_sse_parsing(mcp: WhireMCPClient, recording: RecordingTransport) -> None:
    def sse(request: httpx.Request) -> httpx.Response:
        body = json.dumps({"jsonrpc": "2.0", "id": rpc_request(request)["id"], "result": {"structuredContent": P.CAPABILITIES, "content": []}})
        text = f"event: message\nid: 1\ndata: {body}\n\nevent: message\ndata: {{}}\n\n"
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream; charset=utf-8"})

    recording.push(sse)
    assert await mcp.call_tool("get_capabilities") == P.CAPABILITIES  # the first data: line
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "error": P.MCP_JSONRPC_METHOD_NOT_FOUND})
    recording.push(httpx.Response(200, content=f"data: {body}\n\n".encode(), headers={"content-type": "text/event-stream"}))
    with pytest.raises(MCPProtocolError) as info:
        await mcp.request("nope")
    assert info.value.error_code == "method_not_found"
    recording.push(httpx.Response(200, content=b": keep-alive\n\n", headers={"content-type": "text/event-stream"}))
    with pytest.raises(ResponseFormatError):
        await mcp.ping()


async def test_close_borrowed_http_client_and_invalid_settings(recording: RecordingTransport) -> None:
    mcp = make_mcp_client(recording)
    await mcp.close()
    await mcp.close()
    with pytest.raises(WhireError) as info:
        await mcp.ping()
    assert info.value.error_code == "client_closed" and len(recording) == 0
    recording.push(echo({}))
    async with make_mcp_client(recording) as managed:
        await managed.ping()
    with pytest.raises(WhireError):
        await managed.ping()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: rpc_result({}, rpc_request(r)["id"])))
    shared = WhireMCPClient("https://sandbox.whire.ai", "k", http=http, max_retries=0)
    await shared.ping()
    await shared.close()
    assert http.is_closed is False  # a borrowed AsyncClient is never closed
    await http.aclose()
    for args, kwargs in ((("https://sandbox.whire.ai",), {"auth_scheme": "basic"}), (("https://sandbox.whire.ai",), {"timeout": 0}), (("   ",), {})):
        with pytest.raises(WhireError):
            WhireMCPClient(*args, **kwargs)
    recording.push(tool_answer(P.CAPABILITIES), echo(P.MCP_INITIALIZE_RESULT), httpx.Response(202))
    async with make_client(recording, api_key="shared-key-zzzz") as client:  # WhireClient.mcp shares transport and key
        assert await client.mcp.call_tool("get_capabilities") == P.CAPABILITIES
        assert recording.last.headers["x-api-key"] == "shared-key-zzzz" and recording.last.url == "https://sandbox.whire.ai/mcp"
        await client.mcp.initialize()
        assert client.mcp.protocol_version == "2025-06-18"
    with pytest.raises(WhireError) as closed:
        await client.mcp.ping()
    assert closed.value.error_code == "client_closed"
