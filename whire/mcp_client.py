"""JSON-RPC 2.0 client for the deployment's MCP endpoint (``POST /mcp``).

No dependency on the ``mcp`` package: one HTTP POST per call, plain JSON
replies (SSE bodies are read defensively). Stateless on the server side:
``initialize`` is optional and there is no session id.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Mapping

import httpx

from whire import __version__
from whire._transport import NOT_FOUND_RE, HttpTransport, RawResponse, serialize_body
from whire._validation import _check_production_key, _validate_base_url
from whire.exceptions import (
    AuthenticationError,
    InvalidInputError,
    MCPProtocolError,
    ResponseFormatError,
    ToolError,
    WhireError,
)
from whire.models import (
    Completion,
    PromptInfo,
    PromptMessage,
    ResourceContents,
    ResourceInfo,
    ResourceTemplateInfo,
    ServerInfo,
    ToolDefinition,
)

__all__ = [
    "WhireMCPClient",
    "KNOWN_PROTOCOL_VERSIONS",
    "PROPOSED_PROTOCOL_VERSION",
    "READ_ONLY_TOOLS",
    "MUTATING_TOOLS",
    "LONG_RUNNING_TOOLS",
]

KNOWN_PROTOCOL_VERSIONS: frozenset[str] = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})
PROPOSED_PROTOCOL_VERSION = "2025-06-18"

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
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
)
"""Tools that are safe to repeat: their calls are retried like GETs."""

MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "register_payer",
        "activate_payer_account",
        "suspend_payer_account",
        "add_funding_source",
        "set_default_funding_source",
        "register_user",
        "add_payment_method",
        "set_default_payment_method",
        "create_beneficiary",
        "create_beneficiary_for_user",
        "create_payment_mandate",
        "revoke_payment_mandate",
        "create_payout_draft",
        "submit_payout_for_review",
        "record_provider_event",
        "confirm_payout",
        "execute_payout",
        "pay_x402_resource",
    }
)
"""Tools that change state: never retried once the request may have arrived."""

LONG_RUNNING_TOOLS: frozenset[str] = frozenset({"execute_payout", "pay_x402_resource"})

_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_UNKNOWN_TOOL_PREFIX = "MCP error -32602: Tool "
_INPUT_ERROR_PREFIX = "MCP error -32602: Input validation error"
_INVALID_ARGS_PREFIX = "MCP error -32602"


class WhireMCPClient:
    """Async MCP-over-HTTP client for ``/mcp``.

    Args:
        base_url: Deployment origin (``https://sandbox.whire.ai``) or the full
            ``.../mcp`` URL.
        api_key: API key or ``None`` (no auth header).
        auth_scheme: ``"x-api-key"`` (default) or ``"bearer"``.
        timeout: Read timeout in seconds, or an ``httpx.Timeout``.
        execute_timeout: Read timeout for ``execute_payout`` / ``pay_x402_resource``.
        max_retries, retry_base_delay, retry_max_delay: Retry policy (see README).
        transport: An ``httpx.AsyncBaseTransport`` for tests (``MockTransport``).
        client_info: ``(name, version)`` sent on ``initialize``.
        http: An existing ``httpx.AsyncClient`` to share (not closed by this client).
        auto_idempotency: Send an ``Idempotency-Key`` on every POST (ignored by the
            server today, harmless).
        user_agent: Overrides the default ``whire-python/<v> httpx/<v>``.
        allow_insecure_http: Allow ``http://`` to non-local hosts (the key would travel in clear text).
        allow_unauthenticated: Allow a production (``api.whire.ai``) client without a key.
        http_transport: Internal: a prepared :class:`HttpTransport` to reuse.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        auth_scheme: str = "x-api-key",
        timeout: float | httpx.Timeout = 30.0,
        execute_timeout: float = 120.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client_info: tuple[str, str] = ("whire-python", __version__),
        http: httpx.AsyncClient | None = None,
        auto_idempotency: bool = True,
        user_agent: str | None = None,
        allow_insecure_http: bool = False,
        allow_unauthenticated: bool = False,
        http_transport: HttpTransport | None = None,
    ) -> None:
        origin, path = _split_mcp_url(base_url)
        self._path = path
        self._execute_timeout = execute_timeout
        self._client_info = client_info
        self._ids = itertools.count(1)
        self._server_info: ServerInfo | None = None
        self._protocol_version: str | None = None
        self._init_lock = asyncio.Lock()
        if http_transport is not None:
            self._transport = http_transport
            self._owns_transport = False
            return
        if auth_scheme not in ("x-api-key", "bearer"):
            raise InvalidInputError("auth_scheme must be 'x-api-key' or 'bearer'")
        origin = _validate_base_url(origin, allow_insecure_http)
        _check_production_key(origin, api_key, allow_unauthenticated)
        default_timeout = build_timeout(timeout)
        owns_http = http is None
        if http is None:
            http = httpx.AsyncClient(
                transport=transport,
                headers={"User-Agent": user_agent or default_user_agent()},
                timeout=default_timeout,
            )
        self._transport = HttpTransport(
            http,
            base_url=origin,
            api_key=api_key,
            auth_scheme=auth_scheme,  # type: ignore[arg-type]
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            auto_idempotency=auto_idempotency,
            default_timeout=default_timeout,
            owns_http=owns_http,
        )
        self._owns_transport = True

    # ------------------------------------------------------------------ lifecycle

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    @property
    def endpoint(self) -> str:
        """The full MCP URL."""
        return self._transport.base_url + self._path

    @property
    def protocol_version(self) -> str | None:
        """The negotiated protocol version after :meth:`initialize`, else ``None``."""
        return self._protocol_version

    @property
    def server_info(self) -> ServerInfo | None:
        return self._server_info

    async def close(self) -> None:
        """Close the HTTP client when this instance created it (idempotent)."""
        if self._owns_transport:
            await self._transport.close()

    async def __aenter__(self) -> WhireMCPClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"WhireMCPClient(endpoint={self.endpoint!r}, api_key={self._transport.masked_api_key!r})"

    # ------------------------------------------------------------------ JSON-RPC core

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Send one JSON-RPC request and return its ``result`` (raw escape hatch)."""
        return await self._call(method, dict(params or {}), idempotent=_method_is_idempotent(method, params))

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id); HTTP 202/204 with an empty body is success."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = dict(params)
        raw = await self._post(message, idempotent=True, timeout=None)
        if raw.status in (200, 202, 204) and not raw.body.strip():
            return
        if raw.status >= 400:
            raise self._http_error(raw)
        body = _decode(raw)
        if isinstance(body, dict) and "error" in body:
            raise self._rpc_error(body["error"], raw)

    async def _call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        idempotent: bool,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        return (await self._call_raw(method, params, idempotent=idempotent, timeout=timeout))[0]

    async def _call_raw(
        self,
        method: str,
        params: dict[str, Any],
        *,
        idempotent: bool,
        timeout: httpx.Timeout | None = None,
    ) -> tuple[Any, RawResponse]:
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        raw = await self._post(message, idempotent=idempotent, timeout=timeout)
        if raw.status >= 400:
            raise self._http_error(raw)
        body = _decode(raw)
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            raise ResponseFormatError(
                "MCP reply is not a JSON-RPC 2.0 message", payload=body, request_id=raw.request_id
            )
        if "error" in body:
            raise self._rpc_error(body["error"], raw)
        return body.get("result"), raw

    async def _post(self, message: dict[str, Any], *, idempotent: bool, timeout: httpx.Timeout | None) -> RawResponse:
        headers = dict(_MCP_HEADERS)
        if self._protocol_version is not None:
            headers["MCP-Protocol-Version"] = self._protocol_version
        key = self._transport.resolve_idempotency_key("POST", self._path, None)
        return await self._transport.send(
            "POST",
            self._path,
            content=serialize_body(message),
            headers=headers,
            idempotent=idempotent,
            idempotency_key=key,
            timeout=timeout or self._transport.default_timeout,
        )

    # ------------------------------------------------------------------ errors

    def _http_error(self, raw: RawResponse) -> WhireError:
        common = {"status_code": raw.status, "request_id": raw.request_id, "idempotency_key": raw.idempotency_key}
        body = _decode_or_none(raw)
        if raw.status == 401:
            return AuthenticationError(_auth_message(self._transport, body), **common)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message", f"HTTP {raw.status}"))
            if raw.status == 406:
                message += " (send Accept: application/json, text/event-stream)"
            elif raw.status == 405:
                message += " (the MCP endpoint only accepts POST)"
            return MCPProtocolError(message, code=error.get("code"), data=error.get("data"), **common)
        if isinstance(error, str):
            return WhireError(error, error_code="http_error", **common)
        text = raw.body.decode("utf-8", errors="replace").strip()[:200] or f"HTTP {raw.status}"
        return WhireError(text, error_code="http_error", **common)

    @staticmethod
    def _rpc_error(error: Any, raw: RawResponse) -> MCPProtocolError:
        if not isinstance(error, dict):
            return MCPProtocolError(str(error), status_code=raw.status, request_id=raw.request_id)
        code = error.get("code")
        message = str(error.get("message", "MCP error"))
        error_code = "method_not_found" if code == -32601 else "mcp_error"
        input_error = code == -32603 and bool(NOT_FOUND_RE.search(message)) or code == -32602
        return MCPProtocolError(
            message,
            code=code,
            data=error.get("data"),
            input_error=input_error,
            error_code=error_code,
            status_code=raw.status,
            request_id=raw.request_id,
            idempotency_key=raw.idempotency_key,
        )

    @staticmethod
    def _tool_error(name: str, result: dict[str, Any], raw: RawResponse) -> ToolError:
        text = _first_text(result) or f"tool {name} failed"
        common = {"request_id": raw.request_id, "idempotency_key": raw.idempotency_key}
        if text.startswith(_UNKNOWN_TOOL_PREFIX):
            return ToolError(text, tool_name=name, error_code="unknown_tool", **common)
        if text.startswith(_INPUT_ERROR_PREFIX) or text.startswith(_INVALID_ARGS_PREFIX):
            detail = text.split(":", 2)[2].strip() if text.count(":") >= 2 else None
            return ToolError(text, tool_name=name, error_code="invalid_arguments", data=detail, **common)
        return ToolError(text, tool_name=name, error_code="tool_error", **common)

    # ------------------------------------------------------------------ protocol methods

    async def initialize(self) -> ServerInfo:
        """Negotiate the protocol (cached; safe to call concurrently).

        Sends ``initialize`` then ``notifications/initialized``. Afterwards the
        negotiated ``MCP-Protocol-Version`` header accompanies every request.
        """
        async with self._init_lock:
            if self._server_info is not None:
                return self._server_info
            result = await self._call(
                "initialize",
                {
                    "protocolVersion": PROPOSED_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self._client_info[0], "version": self._client_info[1]},
                },
                idempotent=True,
            )
            info = _server_info(result)
            self._server_info = info
            self._protocol_version = info.protocol_version
            await self.notify("notifications/initialized")
            return info

    async def ping(self) -> None:
        """Round-trip a ``ping``."""
        await self._call("ping", {}, idempotent=True)

    async def list_tools(self) -> list[ToolDefinition]:
        """Return every tool the server offers."""
        result = await self._call("tools/list", {}, idempotent=True)
        return [ToolDefinition.from_wire(item) for item in _items(result, "tools")]

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool and return its structured result.

        ``arguments`` is always sent as an object (the server requires it even
        for zero-argument tools). Returns ``structuredContent`` when present,
        else the parsed JSON of the first text block, else ``{"text": ...}``.

        Raises:
            ToolError: when the server answers ``isError: true``.
            MCPProtocolError: for JSON-RPC or HTTP-level errors.
        """
        idempotent = name in READ_ONLY_TOOLS
        timeout = self._transport.timeout_with_read(self._execute_timeout) if name in LONG_RUNNING_TOOLS else None
        result, raw = await self._call_raw(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
            idempotent=idempotent,
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise ResponseFormatError("tools/call result is not an object", payload=result, request_id=raw.request_id)
        if result.get("isError"):
            raise self._tool_error(name, result, raw)
        return _tool_payload(result)

    async def list_resources(self) -> list[ResourceInfo]:
        """Return every resource the server lists.

        That is the static ``config://provider`` and ``policy://limits`` plus, on
        the sandbox, one ``payout://<id>`` / ``mandate://<id>`` entry per stored
        record. Filter by ``uri`` scheme; never assume two entries.
        """
        result = await self._call("resources/list", {}, idempotent=True)
        return [ResourceInfo.from_wire(item) for item in _items(result, "resources")]

    async def list_resource_templates(self) -> list[ResourceTemplateInfo]:
        """Return the resource templates (``payout://{payoutId}``, ``mandate://{mandateId}``)."""
        result = await self._call("resources/templates/list", {}, idempotent=True)
        return [ResourceTemplateInfo.from_wire(item) for item in _items(result, "resourceTemplates")]

    async def read_resource(self, uri: str) -> ResourceContents:
        """Read a resource; ``.json`` parses the first JSON text content."""
        result = await self._call("resources/read", {"uri": uri}, idempotent=True)
        return ResourceContents.from_wire(result)

    async def list_prompts(self) -> list[PromptInfo]:
        """Return the prompts the server offers."""
        result = await self._call("prompts/list", {}, idempotent=True)
        return [PromptInfo.from_wire(item) for item in _items(result, "prompts")]

    async def get_prompt(self, name: str, arguments: Mapping[str, Any] | None = None) -> list[PromptMessage]:
        """Return the messages of a prompt."""
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = dict(arguments)
        result = await self._call("prompts/get", params, idempotent=True)
        return [PromptMessage.from_wire(item) for item in _items(result, "messages")]

    async def complete(self, ref: Mapping[str, Any], argument: Mapping[str, Any]) -> Completion:
        """``completion/complete`` for a resource template or prompt argument."""
        result = await self._call(
            "completion/complete", {"ref": dict(ref), "argument": dict(argument)}, idempotent=True
        )
        payload = result.get("completion") if isinstance(result, dict) else None
        return Completion.from_wire(payload if isinstance(payload, dict) else {})


# ---------------------------------------------------------------------- helpers


def build_timeout(timeout: float | httpx.Timeout) -> httpx.Timeout:
    """``float`` → ``Timeout(connect=5, read=timeout, write=10, pool=5)``; a Timeout passes through."""
    if isinstance(timeout, httpx.Timeout):
        return timeout
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise InvalidInputError("timeout must be a positive number of seconds or an httpx.Timeout")
    return httpx.Timeout(connect=5.0, read=float(timeout), write=10.0, pool=5.0)


def default_user_agent() -> str:
    """``whire-python/<version> httpx/<version>``."""
    return f"whire-python/{__version__} httpx/{httpx.__version__}"


def _split_mcp_url(base_url: str) -> tuple[str, str]:
    url = base_url.strip().rstrip("/")
    if not url:
        raise InvalidInputError("base_url must not be empty")
    if url.endswith("/mcp"):
        return url[: -len("/mcp")], "/mcp"
    return url, "/mcp"


def _method_is_idempotent(method: str, params: Mapping[str, Any] | None) -> bool:
    if method == "tools/call":
        return str((params or {}).get("name")) in READ_ONLY_TOOLS
    return True


def _decode(raw: RawResponse) -> Any:
    body = _decode_or_none(raw)
    if body is None:
        raise ResponseFormatError(
            "MCP reply is not JSON", payload=raw.body.decode("utf-8", errors="replace")[:200], request_id=raw.request_id
        )
    return body


def _decode_or_none(raw: RawResponse) -> Any:
    text = raw.body.decode("utf-8", errors="replace")
    if "text/event-stream" in raw.content_type:
        text = _first_sse_data(text)
    try:
        return json.loads(text)
    except ValueError:
        return None


def _first_sse_data(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("data:"):
            return line[5:].strip()
    return text


def _items(result: Any, key: str) -> list[dict[str, Any]]:
    items = result.get(key) if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise ResponseFormatError(f"MCP result has no {key!r} list", payload=result)
    return [item for item in items if isinstance(item, dict)]


def _first_text(result: dict[str, Any]) -> str | None:
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return None


def _tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    text = _first_text(result)
    if text is not None:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"text": text}


def _server_info(result: Any) -> ServerInfo:
    if not isinstance(result, dict):
        raise ResponseFormatError("initialize result is not an object", payload=result)
    server = result.get("serverInfo") or {}
    data = {
        "name": server.get("name", "unknown"),
        "version": str(server.get("version", "")),
        "protocolVersion": str(result.get("protocolVersion", "")),
        "capabilities": result.get("capabilities") or {},
        "instructions": result.get("instructions"),
    }
    return ServerInfo.from_wire(data)


def _auth_message(transport: HttpTransport, body: Any) -> str:
    server = None
    if isinstance(body, dict):
        server = body.get("error") if isinstance(body.get("error"), str) else None
    header = "Authorization: Bearer" if transport.auth_scheme == "bearer" else "X-API-Key"
    if not transport.has_api_key:
        base = "Authentication failed: no API key was sent. Set WHIRE_API_KEY or pass api_key=."
    else:
        base = f"Authentication failed: the deployment rejected the key sent as {header}; check WHIRE_API_KEY / api_key."
    return f"{base} Server said: {server}" if server else base
