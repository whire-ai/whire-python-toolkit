"""Shared scaffolding: MockTransport-backed clients, a recording transport, envelope helpers and canned answers.

Nothing here touches the network. ``make_client`` builds a :class:`whire.WhireClient` whose
``httpx.AsyncClient`` runs on a :class:`RecordingTransport`, so every request the SDK sends can be
inspected: method, URL, headers and the exact body bytes.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from tests.fixtures import payloads as P
from whire import Environment, WhireClient
from whire.mcp_client import WhireMCPClient

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
Answer = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response | Exception]

DEFAULT_API_KEY = "test-key-abcd"
RETRY_KW = {"max_retries": 3, "retry_base_delay": 0.01, "retry_max_delay": 0.05}

# --------------------------------------------------------------------------- responses


def json_response(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    """A JSON response with the given plain body (no envelope)."""
    return httpx.Response(status, json=body, headers=headers or {})


def ok(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    """``{"ok": true, "data": data}``."""
    return json_response({"ok": True, "data": data}, status, headers)


def fail(error: str, status: int = 400, headers: dict[str, str] | None = None) -> httpx.Response:
    """``{"ok": false, "error": error}``."""
    return json_response({"ok": False, "error": error}, status, headers)


def replayed(data: Any, status: int = 200) -> httpx.Response:
    """An envelope reply carrying ``idempotent-replay: true``."""
    return ok(data, status, {"idempotent-replay": "true"})


def replayed_error(error: str, status: int = 400) -> httpx.Response:
    """A stored error replayed with ``idempotent-replay: true``."""
    return fail(error, status, {"idempotent-replay": "true"})


def text_response(text: str, status: int = 403, content_type: str = "text/plain") -> httpx.Response:
    """A non-JSON body (Cloudflare's ``error code: 1010`` looks like this)."""
    return httpx.Response(status, content=text.encode(), headers={"content-type": content_type})


def rpc_result(result: Any, request_id: int | None = None, status: int = 200) -> httpx.Response:
    return json_response({"jsonrpc": "2.0", "result": result, "id": request_id}, status)


def rpc_error(code: int, message: str, request_id: int | None = None, data: Any = None, status: int = 200) -> httpx.Response:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return json_response({"jsonrpc": "2.0", "error": error, "id": request_id}, status)


def tool_result(structured: Any, request_id: int | None = None) -> httpx.Response:
    """A successful ``tools/call`` reply: ``structuredContent`` + pretty JSON text."""
    return rpc_result({"content": [{"type": "text", "text": json.dumps(structured, indent=2)}], "structuredContent": structured}, request_id)


def tool_error(text: str, request_id: int | None = None) -> httpx.Response:
    """A ``tools/call`` reply with ``isError: true`` and no ``structuredContent``."""
    return rpc_result({"content": [{"type": "text", "text": text}], "isError": True}, request_id)


def rpc_request(request: httpx.Request) -> dict[str, Any]:
    """Decode the JSON-RPC message an MCP request carries."""
    return json.loads(request.content.decode("utf-8"))


def rpc_reply_for(request: httpx.Request, result: Any = None, *, error: dict[str, Any] | None = None) -> httpx.Response:
    """Answer an MCP request echoing its id (a notification gets HTTP 202)."""
    message = rpc_request(request)
    if "id" not in message:
        return httpx.Response(202)
    if error is not None:
        return rpc_error(error["code"], error["message"], message["id"], error.get("data"))
    return rpc_result(result, message["id"])


def tool_reply_for(request: httpx.Request, structured: Any = None, *, error_text: str | None = None) -> httpx.Response:
    """Answer a ``tools/call`` request echoing its id."""
    message = rpc_request(request)
    return tool_error(error_text, message.get("id")) if error_text is not None else tool_result(structured, message.get("id"))


def tool_answer(structured: Any) -> Handler:
    """A queue entry answering the next ``tools/call`` with ``structured``."""
    return lambda request: tool_reply_for(request, structured)


def tool_failure(text: str) -> Handler:
    """A queue entry answering the next ``tools/call`` with ``isError: true``."""
    return lambda request: tool_reply_for(request, error_text=text)


def answers(tool_name: str | None = None, rest: dict[str, Any] | None = None) -> Handler:
    """A handler answering ``/mcp`` with the fixture of the called tool and REST with ``rest`` (or the tool's fixture)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp":
            return tool_reply_for(request, P.fresh(P.TOOL_RESULTS[rpc_request(request)["params"]["name"]]))
        if rest is not None:
            return ok(P.fresh(rest))
        assert tool_name is not None, "no REST answer configured"
        return ok(P.fresh(P.TOOL_RESULTS[tool_name]))

    return handler


# --------------------------------------------------------------------------- recording transport


@dataclass
class RecordedRequest:
    """One request the SDK sent."""

    method: str
    url: str
    headers: httpx.Headers
    body: bytes

    @property
    def path(self) -> str:
        return httpx.URL(self.url).path

    @property
    def query(self) -> dict[str, str]:
        return dict(httpx.URL(self.url).params)

    @property
    def json(self) -> Any:
        """The decoded JSON body (``None`` when empty)."""
        return json.loads(self.body) if self.body else None

    @property
    def idempotency_key(self) -> str | None:
        return self.headers.get("idempotency-key")

    @property
    def tool_call(self) -> tuple[str, Any]:
        """``(name, arguments)`` of a ``tools/call`` message."""
        return self.json["params"]["name"], self.json["params"]["arguments"]


@dataclass
class RecordingTransport(httpx.AsyncBaseTransport):
    """Records every request; answers come from ``queue`` (a Response, an Exception to raise, or a callable), then ``handler``, then ``default``."""

    queue: deque[Answer] = field(default_factory=deque)
    handler: Handler | None = None
    default: httpx.Response | None = None
    requests: list[RecordedRequest] = field(default_factory=list)

    def push(self, *answers: Answer) -> RecordingTransport:
        self.queue.extend(answers)
        return self

    @property
    def last(self) -> RecordedRequest:
        return self.requests[-1]

    def __len__(self) -> int:
        return len(self.requests)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(RecordedRequest(request.method, str(request.url), httpx.Headers(request.headers), bytes(request.content)))
        answer: Any = self.queue.popleft() if self.queue else self.handler if self.handler is not None else self.default
        assert answer is not None, f"no canned answer for {request.method} {request.url}"
        if callable(answer) and not isinstance(answer, httpx.Response):
            answer = answer(request)
            if asyncio.iscoroutine(answer):
                answer = await answer
        if isinstance(answer, Exception):
            raise answer
        return answer


# --------------------------------------------------------------------------- client factories and fixtures


def _unexpected(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request {request.method} {request.url}")


def _transport(handler: Handler | httpx.AsyncBaseTransport | None) -> httpx.AsyncBaseTransport:
    return handler if isinstance(handler, httpx.AsyncBaseTransport) else httpx.MockTransport(handler or _unexpected)


def make_client(handler: Handler | httpx.AsyncBaseTransport | None = None, **kwargs: Any) -> WhireClient:
    """A ``WhireClient`` on the given transport/handler: test key, sandbox, ``max_retries=0`` unless overridden."""
    kwargs.setdefault("api_key", DEFAULT_API_KEY)
    kwargs.setdefault("environment", Environment.SANDBOX)
    kwargs.setdefault("max_retries", 0)
    return WhireClient(transport=_transport(handler), **kwargs)


def make_mcp_client(handler: Handler | httpx.AsyncBaseTransport | None = None, **kwargs: Any) -> WhireMCPClient:
    """A standalone ``WhireMCPClient`` on the given transport/handler."""
    kwargs.setdefault("api_key", DEFAULT_API_KEY)
    kwargs.setdefault("max_retries", 0)
    return WhireMCPClient("https://sandbox.whire.ai", transport=_transport(handler), **kwargs)


@dataclass
class FakeClock:
    """Replaces ``whire._resources.time`` so polling deadlines are deterministic."""

    now: float = 100.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def recording() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
async def client(recording: RecordingTransport):
    """A client on a fresh :class:`RecordingTransport` (``max_retries=0``)."""
    async with make_client(recording) as instance:
        yield instance


@pytest.fixture
async def retrying_client(recording: RecordingTransport):
    """A client with ``max_retries=3`` and a tiny backoff."""
    async with make_client(recording, **RETRY_KW) as instance:
        yield instance


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch ``asyncio.sleep`` as used by the SDK and record the requested delays."""
    delays: list[float] = []

    async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        delays.append(delay)

    monkeypatch.setattr("whire._transport.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("whire._resources.asyncio.sleep", fake_sleep)
    return delays


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr("whire._resources.time", fake)
    return fake


@pytest.fixture
def seeded_random() -> Iterable[None]:
    """Deterministic jitter for backoff and polling."""
    state = random.getstate()
    random.seed(1234)
    yield
    random.setstate(state)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never inherit WHIRE_* settings from the developer's shell."""
    for name in ("WHIRE_API_KEY", "WHIRE_BASE_URL", "WHIRE_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
