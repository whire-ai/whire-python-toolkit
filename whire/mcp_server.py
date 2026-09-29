"""Stdio MCP server: newline-delimited JSON-RPC over stdin/stdout, backed by :class:`~whire.WhireToolkit`.

Run it with ``python -m whire.mcp_server`` for MCP hosts that cannot reach the
deployment's ``/mcp`` URL directly (Claude Desktop config JSON, clients without
a URL field). Tools are executed through the toolkit (REST where a route exists,
MCP otherwise); resources, prompts and completions are proxied to the
deployment's ``/mcp`` endpoint.

Framing: one JSON-RPC message per line (``json.dumps(msg) + "\\n"``), flushed
after each write. stdout carries only protocol messages; logging goes to stderr.

Environment (all optional unless stated):

- ``WHIRE_API_KEY`` — the key (blank = none). Required with ``WHIRE_ENVIRONMENT=production``:
  the server exits non-zero otherwise.
- ``WHIRE_BASE_URL`` — a self-hosted deployment origin; wins over ``WHIRE_ENVIRONMENT``.
- ``WHIRE_ENVIRONMENT`` — ``sandbox`` (default) or ``production``.
- ``WHIRE_ALLOW_DESTRUCTIVE`` — default ``true``: the MCP host prompts the user per tool
  call, so the server does not gate ``execute_payout`` / ``pay_x402_resource`` again.
  ``false`` makes both answer ``isError`` with ``confirmation_required``.
- ``WHIRE_TIMEOUT`` — read timeout in seconds (default 30); also bounds the drain of
  in-flight requests at shutdown.
- ``WHIRE_MCP_HELPERS`` — ``1`` adds the SDK-only ``wait_for_payout`` tool.
- ``WHIRE_LOG_LEVEL`` — stderr log level (default ``INFO``).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from whire import __version__
from whire.exceptions import MCPProtocolError, WhireError
from whire.mcp_client import KNOWN_PROTOCOL_VERSIONS, PROPOSED_PROTOCOL_VERSION
from whire.toolkit import WhireToolkit

__all__ = [
    "ServerState",
    "StdioServer",
    "ServerConfigError",
    "build_state",
    "handle_message",
    "handle_line",
    "encode",
    "serve_stdio",
    "main",
    "SERVER_NAME",
    "SERVER_CAPABILITIES",
    "PROXIED_METHODS",
    "DEFAULT_TIMEOUT",
]

logger = logging.getLogger("whire.mcp_server")

SERVER_NAME = "whire-python"
DEFAULT_TIMEOUT = 30.0
SERVER_CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": False},
    "resources": {"listChanged": False},
    "prompts": {"listChanged": False},
    "completions": {},
}
PROXIED_METHODS: frozenset[str] = frozenset(
    {
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
        "completion/complete",
    }
)
"""Methods answered by the deployment's ``/mcp`` endpoint, params and result forwarded verbatim."""

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_MAX_LINE = 64 * 1024 * 1024
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", ""})

Writer = Callable[[bytes], Awaitable[None] | None]


class ServerConfigError(ValueError):
    """The environment does not describe a usable server (message goes to stderr, exit 2)."""


# ---------------------------------------------------------------------- state


class ServerState:
    """Everything one server process shares between requests.

    Args:
        toolkit: The toolkit that executes ``tools/call`` and whose client proxies the rest.
        include_helpers: Serve the SDK-only ``wait_for_payout`` tool as well.
        drain_timeout: Seconds to wait for in-flight requests at shutdown.
    """

    def __init__(
        self,
        toolkit: WhireToolkit,
        *,
        include_helpers: bool = False,
        drain_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.toolkit = toolkit
        self.include_helpers = bool(include_helpers)
        self.drain_timeout = float(drain_timeout)
        self.write_lock = asyncio.Lock()
        self.protocol_version: str | None = None
        self.initialized = False
        self.client_info: dict[str, Any] | None = None
        self._tools: list[dict[str, Any]] = toolkit.get_tools("mcp", include_helpers=self.include_helpers)
        self._tool_names: frozenset[str] = frozenset(tool["name"] for tool in self._tools)

    @property
    def tools(self) -> list[dict[str, Any]]:
        """The served tool definitions in MCP format (annotations verbatim)."""
        return self._tools

    @property
    def tool_names(self) -> frozenset[str]:
        """Names a ``tools/call`` may use; anything else answers ``-32602``."""
        return self._tool_names

    async def close(self) -> None:
        await self.toolkit.close()

    def __repr__(self) -> str:
        return f"ServerState(toolkit={self.toolkit!r}, include_helpers={self.include_helpers!r})"


def build_state(environ: Mapping[str, str] | None = None, *, transport: Any = None) -> ServerState:
    """Build the server state from environment variables (see the module docstring).

    Raises:
        ServerConfigError: for an unusable setting, including production without a key.
    """
    env = os.environ if environ is None else environ
    timeout = _parse_timeout(env.get("WHIRE_TIMEOUT"))
    allow_destructive = _parse_bool("WHIRE_ALLOW_DESTRUCTIVE", env.get("WHIRE_ALLOW_DESTRUCTIVE"), default=True)
    include_helpers = _parse_bool("WHIRE_MCP_HELPERS", env.get("WHIRE_MCP_HELPERS"), default=False)
    api_key = env.get("WHIRE_API_KEY")
    base_url = (env.get("WHIRE_BASE_URL") or "").strip() or None
    environment = (env.get("WHIRE_ENVIRONMENT") or "").strip() or None
    try:
        toolkit = WhireToolkit(
            api_key=api_key,
            base_url=base_url,
            environment=environment if base_url is None else None,
            timeout=timeout,
            transport=transport,
            allow_destructive=allow_destructive,
        )
    except WhireError as exc:
        raise ServerConfigError(str(exc)) from exc
    return ServerState(toolkit, include_helpers=include_helpers, drain_timeout=timeout)


def _parse_timeout(value: str | None) -> float:
    text = (value or "").strip()
    if not text:
        return DEFAULT_TIMEOUT
    try:
        seconds = float(text)
    except ValueError:
        raise ServerConfigError(f"WHIRE_TIMEOUT must be a number of seconds, got {value!r}") from None
    if not seconds > 0 or seconds != seconds or seconds == float("inf"):
        raise ServerConfigError(f"WHIRE_TIMEOUT must be a positive number of seconds, got {value!r}")
    return seconds


def _parse_bool(name: str, value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    text = value.strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return default if text == "" else False
    raise ServerConfigError(f"{name} must be true/false (or 1/0), got {value!r}")


# ---------------------------------------------------------------------- JSON-RPC helpers


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def encode(message: Mapping[str, Any] | list[Any]) -> bytes:
    """One protocol line: compact-ish JSON without embedded newlines, UTF-8, trailing ``\\n``."""
    return (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _jsonable_error(exc: WhireError) -> dict[str, Any]:
    return exc.to_agent_dict()


# ---------------------------------------------------------------------- request handling


async def handle_message(server_state: ServerState, message: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; ``None`` means no reply (notifications, responses).

    Never raises: unexpected failures become ``-32603`` replies and are logged to stderr.
    """
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: expected a JSON object")
    request_id = message.get("id")
    has_id = "id" in message
    method = message.get("method")
    if not isinstance(method, str):
        if not has_id or "result" in message or "error" in message:
            return None  # a response to a request we never sent, or garbage without an id: ignore
        return _error(request_id, INVALID_REQUEST, "Invalid Request: 'method' must be a string")
    if not has_id or method.startswith("notifications/"):
        _note_notification(server_state, method, message.get("params"))
        return None
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "Invalid params: 'params' must be an object")
    try:
        return await _dispatch(server_state, request_id, method, params)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the loop must survive any handler failure
        logger.exception("%s failed", method)
        return _error(request_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}: {exc}")


def _note_notification(state: ServerState, method: str, params: Any) -> None:
    if method == "notifications/initialized":
        state.initialized = True
    logger.debug("notification %s", method)


async def _dispatch(state: ServerState, request_id: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return _result(request_id, _initialize(state, params))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": state.tools})
    if method == "tools/call":
        return await _tools_call(state, request_id, params)
    if method in PROXIED_METHODS:
        return await _proxy(state, request_id, method, params)
    return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")


def _initialize(state: ServerState, params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    version = requested if isinstance(requested, str) and requested in KNOWN_PROTOCOL_VERSIONS else PROPOSED_PROTOCOL_VERSION
    state.protocol_version = version
    client_info = params.get("clientInfo")
    state.client_info = dict(client_info) if isinstance(client_info, dict) else None
    logger.info(
        "initialize from %s (requested %r, using %s)",
        (state.client_info or {}).get("name", "unknown client"),
        requested,
        version,
    )
    return {
        "protocolVersion": version,
        "capabilities": copy.deepcopy(SERVER_CAPABILITIES),
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
    }


async def _tools_call(state: ServerState, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or name not in state.tool_names:
        return _error(request_id, INVALID_PARAMS, f"Tool {name} not found")
    arguments = params.get("arguments") or {}
    result = await state.toolkit.execute(name, arguments)
    hint = state.toolkit.hint(name, arguments if isinstance(arguments, Mapping) else None, result)
    if isinstance(result, Mapping) and "error" in result:
        content: list[dict[str, Any]] = [{"type": "text", "text": str(result.get("error"))}]
        if hint:
            content.append({"type": "text", "text": f"Hint: {hint}"})
        logger.info("tools/call %s -> error %s", name, result.get("error_code"))
        return _result(request_id, {"content": content, "isError": True})
    content = [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False, default=str)}]
    if hint:
        content.append({"type": "text", "text": f"Hint: {hint}"})
    logger.info("tools/call %s -> ok", name)
    return _result(request_id, {"content": content, "structuredContent": result})


async def _proxy(state: ServerState, request_id: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await state.toolkit.client.mcp.request(method, params)
    except MCPProtocolError as exc:
        code = exc.code if isinstance(exc.code, int) and not isinstance(exc.code, bool) else INTERNAL_ERROR
        return _error(request_id, code, exc.message, exc.data)
    except WhireError as exc:
        return _error(request_id, INTERNAL_ERROR, exc.message, _jsonable_error(exc))
    return _result(request_id, result if result is not None else {})


Reply = dict[str, Any] | list[dict[str, Any]]


async def handle_line(server_state: ServerState, line: bytes | str) -> list[Reply]:
    """Decode one input line and return the replies to write (possibly none).

    Invalid JSON answers ``-32700`` with ``id: null``; a JSON-RPC batch (array) is
    handled concurrently and answered as one array reply.
    """
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    if not text.strip():
        return []
    try:
        decoded = json.loads(text)
    except ValueError:
        return [_error(None, PARSE_ERROR, "Parse error: Invalid JSON")]
    if isinstance(decoded, list):
        if not decoded:
            return [_error(None, INVALID_REQUEST, "Invalid Request: empty batch")]
        replies = await asyncio.gather(*(handle_message(server_state, item) for item in decoded))
        answered = [reply for reply in replies if reply is not None]
        return [answered] if answered else []
    reply = await handle_message(server_state, decoded)
    return [reply] if reply is not None else []


# ---------------------------------------------------------------------- the stdio loop


class StdioServer:
    """Reads lines from ``reader``, answers each in its own task, writes replies under one lock.

    Args:
        state: Shared server state.
        reader: Source of newline-delimited messages.
        write: Called with each encoded reply (already newline-terminated); sync or async.
    """

    def __init__(self, state: ServerState, *, reader: asyncio.StreamReader, write: Writer) -> None:
        self.state = state
        self._reader = reader
        self._write = write
        self._tasks: set[asyncio.Task[None]] = set()
        self._read_task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def in_flight(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    def stop(self) -> None:
        """Stop reading (SIGTERM/SIGINT); in-flight requests are drained by :meth:`run`."""
        self._stopping = True
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()

    async def run(self) -> None:
        """Serve until EOF or :meth:`stop`, then drain in-flight requests (bounded by ``drain_timeout``)."""
        read_task = asyncio.create_task(self._read_loop(), name="whire-mcp-stdin-loop")
        self._read_task = read_task
        try:
            await read_task
        except asyncio.CancelledError:
            if not read_task.done():
                read_task.cancel()  # run() itself was cancelled from outside
            if not self._stopping:
                raise
        finally:
            self._read_task = None
            await self._drain()

    async def _read_loop(self) -> None:
        while not self._stopping:
            try:
                line = await self._reader.readline()
            except ValueError:  # line longer than the reader's limit: the data is gone
                await self._emit([_error(None, PARSE_ERROR, "Parse error: line too long")])
                continue
            if not line:
                logger.info("stdin closed")
                return
            if not line.strip():
                continue
            task = asyncio.create_task(self._handle(line))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle(self, line: bytes) -> None:
        replies = await handle_line(self.state, line)
        if replies:
            await self._emit(replies)

    async def _emit(self, replies: list[Reply]) -> None:
        async with self.state.write_lock:
            for reply in replies:
                outcome = self._write(encode(reply))
                if outcome is not None:
                    await outcome

    async def _drain(self) -> None:
        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return
        logger.info("draining %d in-flight request(s)", len(pending))
        done, still_pending = await asyncio.wait(pending, timeout=self.state.drain_timeout)
        for task in still_pending:
            task.cancel()
        if still_pending:
            logger.warning("%d request(s) abandoned after %.1fs", len(still_pending), self.state.drain_timeout)
            await asyncio.gather(*still_pending, return_exceptions=True)


async def _stdin_reader() -> asyncio.StreamReader:
    """A StreamReader fed by stdin: through the event loop on pipes/ttys, through a thread otherwise."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=_MAX_LINE)
    stdin = sys.stdin.buffer
    try:
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, stdin)
        return reader
    except (NotImplementedError, PermissionError, OSError, ValueError):
        pass  # Windows, or stdin redirected from a regular file on Linux

    def pump() -> None:
        try:
            for chunk in iter(stdin.readline, b""):
                loop.call_soon_threadsafe(reader.feed_data, chunk)
        finally:
            loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=pump, name="whire-mcp-stdin", daemon=True).start()
    return reader


async def serve_stdio(state: ServerState) -> None:
    """Run the server on this process's stdin/stdout until EOF or SIGTERM/SIGINT, then close the client."""
    stdout = sys.stdout.buffer

    def write(data: bytes) -> None:
        stdout.write(data)
        stdout.flush()

    reader = await _stdin_reader()
    server = StdioServer(state, reader=reader, write=write)
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, server.stop)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # not supported on this platform / not the main thread
    try:
        await server.run()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        await state.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m whire.mcp_server``; returns the exit code."""
    level = (os.environ.get("WHIRE_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        state = build_state()
    except ServerConfigError as exc:
        print(f"{exc}", file=sys.stderr, flush=True)
        return 2
    logger.info(
        "whire.mcp_server %s serving %d tool(s) against %s", __version__, len(state.tools), state.toolkit.client.base_url
    )
    try:
        asyncio.run(serve_stdio(state))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
