"""The stdio MCP server (``whire/mcp_server.py``): framing, JSON-RPC dispatch, proxying, concurrency, env contract (SPEC §8).

The toolkit behind the server records on a ``RecordingTransport``; nothing touches the network. The
one subprocess test runs ``python -m whire.mcp_server`` for real with messages that never reach a deployment.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import DEFAULT_API_KEY, RecordingTransport, fail, json_response, make_client, ok, rpc_error, rpc_reply_for, rpc_request, tool_reply_for
from tests.fixtures import payloads as P
from whire import __version__
from whire.mcp_client import KNOWN_PROTOCOL_VERSIONS
from whire.mcp_server import DEFAULT_TIMEOUT, PROXIED_METHODS, SERVER_CAPABILITIES, ServerConfigError, ServerState, StdioServer, build_state, encode, handle_line, handle_message, main
from whire.toolkit import WhireToolkit
from whire.tools import DESTRUCTIVE_TOOLS, TOOL_NAMES, TOOLS, WAIT_FOR_PAYOUT_TOOL

REPO = Path(__file__).resolve().parents[1]
SECRET_KEY = "sk-live-secret-key-9f8e7d6c"
CALL_CAPABILITIES = {"name": "get_capabilities", "arguments": {}}
PARSE_ERROR_REPLY = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error: Invalid JSON"}}

PROXY_RESULTS: dict[str, dict[str, Any]] = {
    "resources/list": P.MCP_RESOURCES_LIST_RESULT, "resources/templates/list": P.MCP_TEMPLATES_LIST_RESULT,
    "resources/read": P.MCP_RESOURCE_READ_RESULT, "prompts/list": P.MCP_PROMPTS_LIST_RESULT,
    "prompts/get": P.MCP_PROMPT_GET_RESULT, "completion/complete": P.MCP_COMPLETION_RESULT,
}
PROXY_PARAMS: dict[str, dict[str, Any]] = {
    "resources/list": {}, "resources/templates/list": {}, "resources/read": {"uri": "config://provider"}, "prompts/list": {},
    "prompts/get": {"name": "run_agent_payout_flow", "arguments": {"beneficiaryName": "Alice"}},
    "completion/complete": {"ref": {"type": "ref/resource", "uri": "payout://{payoutId}"}, "argument": {"name": "payoutId", "value": "70"}},
}


def req(request_id: Any, method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    return message if params is None else {**message, "params": params}


def notification(method: str, params: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method} if params is None else {"jsonrpc": "2.0", "method": method, "params": params}


def default_handler(request: httpx.Request) -> httpx.Response:
    """REST fixtures by path; ``/mcp`` answered from the tool fixtures or the proxied-method fixtures."""
    path = request.url.path
    if path == "/mcp":
        message = rpc_request(request)
        if message["method"] == "tools/call":
            return tool_reply_for(request, P.fresh(P.TOOL_RESULTS[message["params"]["name"]]))
        return rpc_reply_for(request, P.fresh(PROXY_RESULTS[message["method"]]))
    rest = {
        "/api/capabilities": ok(P.fresh(P.CAPABILITIES)),
        "/api/payers": ok(P.fresh(P.PAYERS_LIST)),
        f"/api/payers/{P.PAYER_ID}": ok(P.fresh(P.PAYER_ACTIVE)),
        "/api/payers/nope": fail(P.ERROR_PAYER_NOT_FOUND, 400),
        f"/api/payouts/{P.PAYOUT_ID}": ok(P.fresh(P.PAYOUT_PAID)),
        f"/api/payouts/{P.PAYOUT_ID}/execute": ok(P.fresh(P.EXECUTION_PAID)),
    }
    assert path in rest, f"unexpected request {request.method} {path}"
    return rest[path]


def state_for(recording: RecordingTransport, **kwargs: Any) -> ServerState:
    """A server around a toolkit whose client records on ``recording`` (default handler unless set)."""
    if recording.handler is None and recording.default is None:
        recording.handler = default_handler
    toolkit_kwargs = {key: kwargs.pop(key) for key in ("allow_destructive", "confirm") if key in kwargs}
    return ServerState(WhireToolkit(client=make_client(recording), **toolkit_kwargs), **kwargs)


def gated_handler(gate: asyncio.Event, started: asyncio.Event) -> Callable[[httpx.Request], Any]:
    """The capabilities read waits for ``gate``; everything else is answered at once."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/capabilities":
            started.set()
            await gate.wait()
            return ok(P.fresh(P.CAPABILITIES))
        return default_handler(request)

    return handler


class Collector:
    """A ``write`` callback for :class:`StdioServer` that keeps every line and notes the lock state."""

    def __init__(self, state: ServerState) -> None:
        self.state, self.lines, self.locked_during_write = state, [], []  # type: ignore[var-annotated]

    def __call__(self, data: bytes) -> None:
        self.locked_during_write.append(self.state.write_lock.locked())
        self.lines.append(data)

    @property
    def messages(self) -> list[Any]:
        return [json.loads(line) for line in self.lines]

    async def wait_for(self, count: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.lines) < count:
            assert time.monotonic() < deadline, f"expected {count} line(s), got {len(self.lines)}"
            await asyncio.sleep(0.005)


def start_server(state: ServerState) -> tuple[StdioServer, asyncio.StreamReader, Collector, asyncio.Task[None]]:
    reader, collector = asyncio.StreamReader(), Collector(state)
    server = StdioServer(state, reader=reader, write=collector)
    return server, reader, collector, asyncio.create_task(server.run())


# --------------------------------------------------------------------------- framing, initialize, ping, notifications


async def test_newline_framing_parse_errors_and_batches(recording: RecordingTransport) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "result": {"text": "multi\nline café"}}
    data = encode(message)
    assert data == (json.dumps(message, ensure_ascii=False) + "\n").encode() and data.count(b"\n") == 1
    state = state_for(recording)
    assert await handle_line(state, b'{"jsonrpc": "2.0", "id": 1, "method": ') == [PARSE_ERROR_REPLY]
    assert await handle_line(state, b"   \n") == [] and await handle_line(state, "") == []
    (reply,) = await handle_line(state, b"42\n")
    assert reply["id"] is None and reply["error"]["code"] == -32600
    (batch,) = await handle_line(state, json.dumps([req(1, "ping"), notification("notifications/cancelled"), req("b", "nope")]))
    assert batch == [{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "id": "b", "error": {"code": -32601, "message": "Method not found: nope"}}]
    assert await handle_line(state, json.dumps([notification("notifications/initialized")])) == []
    (empty,) = await handle_line(state, b"[]")
    assert empty["error"]["code"] == -32600 and empty["id"] is None


async def test_initialize_echoes_known_versions_and_reports_capabilities(recording: RecordingTransport) -> None:
    for version in sorted(KNOWN_PROTOCOL_VERSIONS):
        state = state_for(recording)
        reply = await handle_message(state, req(1, "initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}))
        assert reply is not None and reply["result"]["protocolVersion"] == version and state.protocol_version == version
    for params in ({"protocolVersion": "1999-01-01"}, {"protocolVersion": 7}, {}, None):
        reply = await handle_message(state_for(recording), req(1, "initialize", params))
        assert reply is not None and reply["result"]["protocolVersion"] == "2025-06-18"
    state = state_for(recording)
    reply = await handle_message(state, req(1, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-desktop", "version": "1.2"}}))
    result = reply["result"]  # type: ignore[index]
    expected = {"tools": {"listChanged": False}, "resources": {"listChanged": False}, "prompts": {"listChanged": False}, "completions": {}}
    assert result["capabilities"] == SERVER_CAPABILITIES == expected
    assert result["capabilities"] is not SERVER_CAPABILITIES and result["serverInfo"] == {"name": "whire-python", "version": __version__}
    assert "instructions" not in result and state.client_info == {"name": "claude-desktop", "version": "1.2"} and not recording.requests


async def test_ping_notifications_invalid_requests_and_unknown_methods(recording: RecordingTransport) -> None:
    state = state_for(recording)
    for request_id in (1, "abc", 0):
        assert await handle_message(state, req(request_id, "ping")) == {"jsonrpc": "2.0", "id": request_id, "result": {}}
    assert state.initialized is False
    for message in (
        notification("notifications/initialized"),
        notification("notifications/cancelled", {"requestId": 1, "reason": "user"}),
        notification("tools/call", CALL_CAPABILITIES),  # no id: a notification whatever the method
        {"jsonrpc": "2.0", "id": 9, "method": "notifications/initialized"},  # an id does not make it a request
        {"jsonrpc": "2.0", "id": 1, "result": {}},  # a response to a request we never sent
        {"jsonrpc": "2.0"},
    ):
        assert await handle_message(state, message) is None, message
    assert state.initialized is True and not recording.requests
    reply = await handle_message(state, {"jsonrpc": "2.0", "id": 3, "method": 5})
    assert reply is not None and reply["error"]["code"] == -32600
    reply = await handle_message(state, req(3, "ping", [1, 2]))
    assert reply is not None and reply["error"]["code"] == -32602
    for method in ("nope", "tools/nope", "resources/subscribe", "sampling/createMessage"):
        assert await handle_message(state, req(5, method, {})) == {"jsonrpc": "2.0", "id": 5, "error": {"code": -32601, "message": f"Method not found: {method}"}}


# --------------------------------------------------------------------------- tools/list and tools/call


async def test_tools_list_serves_the_33_tools_and_the_helper_only_when_enabled(recording: RecordingTransport) -> None:
    state = state_for(recording)
    reply = await handle_message(state, req(1, "tools/list"))
    tools = reply["result"]["tools"]  # type: ignore[index]
    assert len(tools) == 33 and [tool["name"] for tool in tools] == list(TOOL_NAMES) and tools == state.toolkit.get_tools("mcp")
    by_name = {tool["name"]: tool for tool in tools}
    for tool in TOOLS:
        served = by_name[tool["name"]]
        assert served["inputSchema"]["$schema"] == "http://json-schema.org/draft-07/schema#" and served["outputSchema"]["additionalProperties"] is False
        assert served.get("annotations") == tool["annotations"] and ("annotations" in served) is (tool["name"] in DESTRUCTIVE_TOOLS)  # verbatim
    tools[0]["inputSchema"]["properties"].clear()
    assert TOOLS[0]["input_schema"]["properties"] and not recording.requests  # the reply is isolated from the definitions
    with_helpers = ServerState(state.toolkit, include_helpers=True)
    tools = (await handle_message(with_helpers, req(2, "tools/list")))["result"]["tools"]  # type: ignore[index]
    assert len(tools) == 34 and tools[-1]["name"] == "wait_for_payout" and tools[-1]["annotations"] == WAIT_FOR_PAYOUT_TOOL["annotations"]
    assert with_helpers.tool_names == frozenset(TOOL_NAMES) | {"wait_for_payout"}


async def test_tools_call_success_hint_block_and_argument_handling(recording: RecordingTransport) -> None:
    state = state_for(recording)
    reply = await handle_message(state, req(1, "tools/call", CALL_CAPABILITIES))
    result = reply["result"]  # type: ignore[index]
    assert set(result) == {"content", "structuredContent"} and result["structuredContent"] == P.CAPABILITIES and "isError" not in result
    assert result["content"][0] == {"type": "text", "text": json.dumps(P.CAPABILITIES, indent=2)}
    hint = state.toolkit.hint("get_capabilities", {}, P.CAPABILITIES)
    assert hint and result["content"][1] == {"type": "text", "text": f"Hint: {hint}"} and len(result["content"]) == 2
    assert recording.last.method == "GET" and recording.last.path == "/api/capabilities"
    for params in ({"name": "list_payers"}, {"name": "list_payers", "arguments": None}):  # missing arguments default to {}
        reply = await handle_message(state, req(1, "tools/call", params))
        assert reply["result"]["structuredContent"] == P.PAYERS_LIST and reply["result"]["content"] == [{"type": "text", "text": json.dumps(P.PAYERS_LIST, indent=2)}]  # type: ignore[index]
    reply = await handle_message(state, req(1, "tools/call", {"name": "get_payer", "arguments": {"payer_id": P.PAYER_ID}}))  # snake_case accepted
    assert reply["result"]["structuredContent"] == P.PAYER_ACTIVE and recording.last.path == f"/api/payers/{P.PAYER_ID}"  # type: ignore[index]
    reply = await handle_message(state, req(1, "tools/call", {"name": "get_user", "arguments": {"userId": P.USER_ID}}))  # MCP-backed tools proxy
    assert reply["result"]["structuredContent"] == P.USER and recording.last.tool_call == ("get_user", {"userId": P.USER_ID})  # type: ignore[index]
    sent = len(recording)
    for arguments in ([P.PAYER_ID], {}):  # local validation failures are results, not JSON-RPC errors
        reply = await handle_message(state, req(1, "tools/call", {"name": "get_payer", "arguments": arguments}))
        assert reply["result"]["isError"] is True and "structuredContent" not in reply["result"]  # type: ignore[index]
    assert "payerId" in reply["result"]["content"][0]["text"] and len(recording) == sent  # type: ignore[index]


async def test_tools_call_errors_gating_and_unknown_tools(recording: RecordingTransport) -> None:
    state = state_for(recording)
    reply = await handle_message(state, req(1, "tools/call", {"name": "get_payer", "arguments": {"payerId": "nope"}}))
    result = reply["result"]  # type: ignore[index]
    assert "error" not in reply and result["isError"] is True and "structuredContent" not in result  # type: ignore[operator]
    assert result["content"][0] == {"type": "text", "text": P.ERROR_PAYER_NOT_FOUND}
    assert all(block["type"] == "text" and block["text"].startswith("Hint: ") for block in result["content"][1:])
    execute = {"name": "execute_payout", "arguments": {"payoutId": P.PAYOUT_ID}}
    reply = await handle_message(state_for(recording, allow_destructive=True), req(1, "tools/call", execute))
    assert reply["result"]["structuredContent"] == P.EXECUTION_PAID and recording.last.path == f"/api/payouts/{P.PAYOUT_ID}/execute"  # type: ignore[index]
    assert recording.last.idempotency_key  # keyed, so a network retry can never double-send
    sent = len(recording)
    reply = await handle_message(state_for(recording, allow_destructive=False), req(1, "tools/call", execute))
    assert reply["result"]["isError"] is True and "requires human confirmation" in reply["result"]["content"][0]["text"] and len(recording) == sent  # type: ignore[index]
    for name in ("nope", "reset", "simulation.reset", "", None):
        reply = await handle_message(state, req(1, "tools/call", {"name": name, "arguments": {}}))
        assert reply == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": f"Tool {name} not found"}}
    assert (await handle_message(state, req(1, "tools/call")))["error"]["code"] == -32602  # type: ignore[index]
    helper = {"name": "wait_for_payout", "arguments": {"payoutId": P.PAYOUT_ID}}
    assert (await handle_message(state, req(1, "tools/call", helper)))["error"]["code"] == -32602  # only served tools are callable  # type: ignore[index]
    reply = await handle_message(ServerState(state.toolkit, include_helpers=True), req(2, "tools/call", helper))
    assert reply["result"]["structuredContent"] == P.PAYOUT_PAID and reply["result"]["content"][1]["text"].startswith("Hint: Paid")  # type: ignore[index]

    async def boom(name: str, arguments: Any = None) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    state.toolkit.execute = boom  # type: ignore[method-assign]
    reply = await handle_message(state, req(1, "tools/call", {"name": "get_capabilities"}))
    assert reply["error"]["code"] == -32603 and "kaboom" in reply["error"]["message"]  # an unexpected exception never crashes the loop  # type: ignore[index]


# --------------------------------------------------------------------------- proxied methods


async def test_proxied_methods_forward_params_results_and_errors_verbatim(recording: RecordingTransport) -> None:
    assert PROXIED_METHODS == frozenset(PROXY_RESULTS) == frozenset(PROXY_PARAMS)
    state = state_for(recording)
    for method in sorted(PROXIED_METHODS):
        reply = await handle_message(state, req(11, method, P.fresh(PROXY_PARAMS[method])))
        assert reply == {"jsonrpc": "2.0", "id": 11, "result": PROXY_RESULTS[method]}, method
        assert recording.last.path == "/mcp" and recording.last.json["method"] == method and recording.last.json["params"] == PROXY_PARAMS[method]
        assert recording.last.headers["accept"] == "application/json, text/event-stream" and recording.last.headers["x-api-key"] == DEFAULT_API_KEY
    reply = await handle_message(state, req(1, "resources/read", {"uri": "config://provider"}))
    content = reply["result"]["contents"][0]  # type: ignore[index]
    assert content["mediaType"] == "application/json" and json.loads(content["text"])["simulated"] is True  # the server's own key, untouched
    await handle_message(state, req(1, "resources/list"))
    assert recording.last.json["params"] == {}  # missing params are sent as an empty object
    for error, code in ((P.MCP_JSONRPC_RESOURCE_MISSING, -32603), (P.MCP_JSONRPC_UNKNOWN_SCHEME, -32602), (P.MCP_JSONRPC_METHOD_NOT_FOUND, -32601)):
        recording.handler = lambda request, error=error: rpc_reply_for(request, error=error)
        reply = await handle_message(state_for(recording), req(4, "resources/read", {"uri": "payout://nope"}))
        assert reply == {"jsonrpc": "2.0", "id": 4, "error": {"code": code, "message": error["message"]}}
    recording.handler = lambda request: rpc_error(-32602, "bad", rpc_request(request)["id"], data={"path": ["uri"]})
    reply = await handle_message(state_for(recording), req(4, "resources/read", {"uri": 5}))
    assert reply["error"] == {"code": -32602, "message": "bad", "data": {"path": ["uri"]}}  # type: ignore[index]
    recording.handler = lambda request: json_response({"error": "Unauthorized"}, 401)
    reply = await handle_message(state_for(recording), req(4, "prompts/list", {}))
    assert reply["error"]["code"] == -32603 and reply["error"]["data"]["error_code"] == "auth_failed" and DEFAULT_API_KEY not in json.dumps(reply)  # type: ignore[index]
    recording.handler = lambda request: rpc_error(-32000, P.MCP_JSONRPC_NOT_ACCEPTABLE["message"], None, status=406)
    reply = await handle_message(state_for(recording), req(4, "prompts/list", {}))
    assert reply["error"]["code"] == -32000 and "Accept" in reply["error"]["message"]  # type: ignore[index]


# --------------------------------------------------------------------------- concurrency and the stdio loop


async def test_stalled_tools_call_does_not_delay_ping(recording: RecordingTransport) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    recording.handler = gated_handler(gate, started)
    state = state_for(recording)
    call = asyncio.create_task(handle_message(state, req(1, "tools/call", CALL_CAPABILITIES)))
    await asyncio.wait_for(started.wait(), 1.0)
    ping = await asyncio.wait_for(handle_message(state, req(2, "ping")), 1.0)
    assert ping == {"jsonrpc": "2.0", "id": 2, "result": {}} and not call.done()
    gate.set()
    reply = await asyncio.wait_for(call, 1.0)
    assert reply is not None and reply["result"]["structuredContent"] == P.CAPABILITIES


async def test_loop_runs_each_line_in_its_own_task_and_drains_on_eof(recording: RecordingTransport) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    recording.handler = gated_handler(gate, started)
    server, reader, out, run = start_server(state_for(recording, drain_timeout=5.0))
    reader.feed_data(b"\n")
    reader.feed_data(b"not json\n")
    reader.feed_data(encode(notification("notifications/initialized")))
    reader.feed_data(encode(req(1, "tools/call", CALL_CAPABILITIES)))
    await asyncio.wait_for(started.wait(), 1.0)
    reader.feed_data(encode(req(2, "ping")))
    reader.feed_data(encode(req(3, "tools/list")))
    await out.wait_for(3)
    assert out.messages[0] == PARSE_ERROR_REPLY and [m["id"] for m in out.messages[1:]] == [2, 3] and server.in_flight == 1
    reader.feed_eof()
    assert server.in_flight == 1  # EOF waits for the in-flight call
    gate.set()
    await asyncio.wait_for(run, 1.0)
    assert out.messages[3]["id"] == 1 and out.messages[3]["result"]["structuredContent"] == P.CAPABILITIES
    assert all(out.locked_during_write) and all(line.endswith(b"\n") and line.count(b"\n") == 1 for line in out.lines)


async def test_stop_and_bounded_drain(recording: RecordingTransport) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    recording.handler = gated_handler(gate, started)
    server, reader, out, run = start_server(state_for(recording, drain_timeout=5.0))
    reader.feed_data(encode(req(1, "tools/call", CALL_CAPABILITIES)))
    await asyncio.wait_for(started.wait(), 1.0)
    server.stop()  # what SIGTERM does
    assert server.in_flight == 1
    gate.set()
    await asyncio.wait_for(run, 1.0)
    assert [m["id"] for m in out.messages] == [1]
    gate, started = asyncio.Event(), asyncio.Event()
    recording.handler = gated_handler(gate, started)
    server, reader, out, run = start_server(state_for(recording, drain_timeout=0.05))
    reader.feed_data(encode(req(1, "tools/call", CALL_CAPABILITIES)))
    await asyncio.wait_for(started.wait(), 1.0)
    reader.feed_eof()
    await asyncio.wait_for(run, 2.0)  # bounded: returns after drain_timeout, not after the gate
    assert out.messages == [] and server.in_flight == 0  # the abandoned call never answers
    gate.set()


# --------------------------------------------------------------------------- environment contract and the real process


async def test_environment_contract(recording: RecordingTransport, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    state = build_state({}, transport=recording)
    assert state.toolkit.client.base_url == "https://sandbox.whire.ai" and state.toolkit.allow_destructive is True
    assert state.include_helpers is False and state.drain_timeout == DEFAULT_TIMEOUT == 30.0 and len(state.tools) == 33
    state = build_state({"WHIRE_ENVIRONMENT": "production", "WHIRE_API_KEY": SECRET_KEY}, transport=recording)
    assert state.toolkit.client.base_url == "https://api.whire.ai" and SECRET_KEY not in repr(state) and SECRET_KEY not in repr(state.toolkit)
    state = build_state({"WHIRE_BASE_URL": "https://payments.example.test/", "WHIRE_ENVIRONMENT": "production"}, transport=recording)
    assert state.toolkit.client.base_url == "https://payments.example.test"  # base URL wins; no key needed off api.whire.ai
    for env in ({"WHIRE_ENVIRONMENT": "production"}, {"WHIRE_ENVIRONMENT": "PRODUCTION", "WHIRE_API_KEY": "   "}, {"WHIRE_BASE_URL": "https://api.whire.ai"}):
        with pytest.raises(ServerConfigError, match="WHIRE_API_KEY"):
            build_state(env, transport=recording)
    for env in ({"WHIRE_ENVIRONMENT": "staging"}, {"WHIRE_BASE_URL": "http://payments.example.test"}, {"WHIRE_ALLOW_DESTRUCTIVE": "maybe"}, {"WHIRE_TIMEOUT": "-3"}):
        with pytest.raises(ServerConfigError):
            build_state(env, transport=recording)
    recording.handler = default_handler
    state = build_state({"WHIRE_API_KEY": SECRET_KEY}, transport=recording)
    await handle_message(state, req(1, "tools/call", {"name": "get_capabilities"}))
    assert recording.last.headers["x-api-key"] == SECRET_KEY
    for value, expected in (("false", False), ("0", False), ("OFF", False), ("true", True), ("yes", True), ("", True)):
        assert build_state({"WHIRE_ALLOW_DESTRUCTIVE": value}, transport=recording).toolkit.allow_destructive is expected, value
    sent = len(recording)
    gated = build_state({"WHIRE_ALLOW_DESTRUCTIVE": "false"}, transport=recording)
    reply = await handle_message(gated, req(1, "tools/call", {"name": "execute_payout", "arguments": {"payoutId": P.PAYOUT_ID}}))
    assert reply["result"]["isError"] is True and len(recording) == sent  # type: ignore[index]
    for value, expected in (("1", True), ("true", True), ("0", False), ("", False)):
        state = build_state({"WHIRE_MCP_HELPERS": value}, transport=recording)
        assert state.include_helpers is expected and ("wait_for_payout" in state.tool_names) is expected and len(state.tools) == (34 if expected else 33)
    state = build_state({"WHIRE_TIMEOUT": "7.5"}, transport=recording)
    assert state.drain_timeout == 7.5 and state.toolkit.client._http.timeout.read == 7.5
    monkeypatch.setenv("WHIRE_MCP_HELPERS", "1")
    assert build_state(transport=recording).include_helpers is True  # os.environ by default
    monkeypatch.setenv("WHIRE_ENVIRONMENT", "production")
    assert main() == 2  # production without a key: exit non-zero, message on stderr, stdout untouched
    captured = capsys.readouterr()
    assert "WHIRE_API_KEY" in captured.err and captured.out == ""


def test_stdio_round_trip_in_a_real_process() -> None:
    lines = [
        json.dumps(req(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})),
        json.dumps(notification("notifications/initialized")),
        json.dumps(req(2, "ping")),
        "{this is not json",
        json.dumps(req(3, "tools/list")),
        json.dumps(req(4, "nope")),
        json.dumps(req(5, "tools/call", {"name": "reset"})),
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHIRE_")}
    env.update({"WHIRE_ENVIRONMENT": "sandbox", "WHIRE_API_KEY": SECRET_KEY, "WHIRE_MCP_HELPERS": "1", "PYTHONUNBUFFERED": "1"})
    completed = subprocess.run(
        [sys.executable, "-m", "whire.mcp_server"], input="".join(line + "\n" for line in lines), capture_output=True, text=True, env=env, cwd=REPO, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    out_lines = completed.stdout.splitlines()
    assert len(out_lines) == 6, completed.stdout
    by_id = {reply["id"]: reply for reply in (json.loads(line) for line in out_lines)}  # every stdout line is one JSON message
    assert by_id[1]["result"]["protocolVersion"] == "2024-11-05" and by_id[1]["result"]["serverInfo"] == {"name": "whire-python", "version": __version__}
    assert by_id[1]["result"]["capabilities"] == SERVER_CAPABILITIES and by_id[2]["result"] == {} and by_id[None] == PARSE_ERROR_REPLY
    assert len(by_id[3]["result"]["tools"]) == 34 and by_id[4]["error"]["code"] == -32601 and by_id[5]["error"]["code"] == -32602
    assert "whire.mcp_server" in completed.stderr and SECRET_KEY not in completed.stderr and SECRET_KEY not in completed.stdout  # logging goes to stderr
    env["WHIRE_ENVIRONMENT"] = "production"
    del env["WHIRE_API_KEY"]
    completed = subprocess.run([sys.executable, "-m", "whire.mcp_server"], input=lines[2] + "\n", capture_output=True, text=True, env=env, cwd=REPO, timeout=60)
    assert completed.returncode != 0 and "WHIRE_API_KEY" in completed.stderr and completed.stdout == ""
