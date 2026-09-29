"""HTTP transport: base URL, auth, envelope unwrap, error mapping, retries, idempotency.

One :class:`HttpTransport` wraps one ``httpx.AsyncClient``. The REST
namespaces call :meth:`HttpTransport.request`; the MCP client calls
:meth:`HttpTransport.send` and interprets JSON-RPC bodies itself. The API key
lives only here, as a private attribute, and is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Mapping

import httpx

from whire.exceptions import (
    AmbiguousResponseError,
    AuthenticationError,
    BadRequestError,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ResponseFormatError,
    ServerError,
    WhireError,
)

__all__ = ["HttpTransport", "RawResponse", "TransportResult", "NOT_FOUND_RE", "serialize_body"]

logger = logging.getLogger("whire")

NOT_FOUND_RE = re.compile(r"\b(?:not found|does not exist)\.?$", re.IGNORECASE)
"""Matches the server's ``"<Kind> <id> not found."`` / ``"<Kind> <id> does not exist."`` sentences."""

AuthScheme = Literal["x-api-key", "bearer"]

_REQUEST_ID_HEADERS = ("x-railway-request-id", "x-request-id", "cf-ray")
_IDEMPOTENT_PATHS = frozenset({"/x402/settle"})
_KEYED_PATH_PREFIXES = ("/api/", "/x402/verify", "/x402/settle", "/mcp")
_RETRY_STATUSES = frozenset({409, 429, 500, 502, 503, 504})
_SAFE_STATUSES = frozenset({429, 503})  # the request never reached the handler
_AMBIGUOUS_STATUSES = frozenset({500, 502, 504})
_SAFE_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)
_AMBIGUOUS_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)


def serialize_body(payload: Any) -> bytes:
    """Serialize a JSON body exactly once (compact separators, UTF-8, no ASCII escaping).

    This serialization is part of the SDK contract: the same bytes are sent on
    every retry, and the server compares idempotent replays byte for byte.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RawResponse:
    """An HTTP response after retries, before any body interpretation."""

    status: int
    headers: httpx.Headers
    body: bytes
    request_id: str | None
    replayed: bool
    idempotency_key: str | None
    attempts: int

    def json(self) -> Any:
        """Decode the body as JSON (``ValueError`` when it is not JSON)."""
        return json.loads(self.body.decode("utf-8"))

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass(frozen=True, slots=True)
class TransportResult:
    """The outcome of a successful REST call."""

    data: Any
    raw_body: bytes
    status: int
    headers: httpx.Headers
    replayed: bool
    request_id: str | None
    idempotency_key: str | None


class HttpTransport:
    """Retrying, idempotency-aware HTTP layer over one ``httpx.AsyncClient``.

    Args:
        http: The shared async client (created by :class:`~whire.client.WhireClient`).
        base_url: Deployment origin without a trailing slash.
        api_key: The key, or ``None`` to send no auth header at all.
        auth_scheme: ``"x-api-key"`` or ``"bearer"``.
        max_retries: Extra attempts after the first (0–10).
        retry_base_delay: Base of the exponential backoff, seconds.
        retry_max_delay: Cap for backoff and for honoured ``Retry-After``.
        auto_idempotency: Generate an ``Idempotency-Key`` for every POST.
        default_timeout: The ``httpx.Timeout`` used unless overridden.
        owns_http: Whether :meth:`close` closes ``http``.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str | None,
        auth_scheme: AuthScheme = "x-api-key",
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 10.0,
        auto_idempotency: bool = True,
        default_timeout: httpx.Timeout,
        owns_http: bool = True,
    ) -> None:
        self._http = http
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.auth_scheme: AuthScheme = auth_scheme
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.auto_idempotency = auto_idempotency
        self.default_timeout = default_timeout
        self._owns_http = owns_http
        self._closed = False
        self._warned_no_key = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_api_key(self) -> bool:
        return self._api_key is not None

    @property
    def masked_api_key(self) -> str | None:
        """``"…abcd"`` (last four characters) or ``None``; safe for ``repr``."""
        if self._api_key is None:
            return None
        return "…" + self._api_key[-4:]

    async def close(self) -> None:
        """Close the underlying client (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()

    def _check_open(self) -> None:
        if self._closed:
            raise WhireError("client is closed", error_code="client_closed")

    # ------------------------------------------------------------------ helpers

    def timeout_with_read(self, read: float | None) -> httpx.Timeout:
        """A copy of the default timeout with a different read timeout."""
        base = self.default_timeout
        if read is None:
            return base
        return httpx.Timeout(connect=base.connect, read=read, write=base.write, pool=base.pool)

    def _auth_headers(self) -> dict[str, str]:
        if self._api_key is None:
            return {}
        if self.auth_scheme == "bearer":
            return {"Authorization": f"Bearer {self._api_key}"}
        return {"X-API-Key": self._api_key}

    def _warn_if_unauthenticated(self) -> None:
        if self._api_key is None and not self._warned_no_key:
            self._warned_no_key = True
            logger.warning(
                "whire: no API key configured; this only works while the deployment runs with authentication off"
            )

    def resolve_idempotency_key(self, method: str, path: str, idempotency_key: str | None) -> str | None:
        """The key to send: the caller's, else a fresh uuid4 for keyed POST paths."""
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise InvalidInputError("idempotency_key must be a non-blank string")
            return idempotency_key.strip()
        if method.upper() != "POST" or not self.auto_idempotency:
            return None
        if path.startswith(_KEYED_PATH_PREFIXES):
            return str(uuid.uuid4())
        return None

    @staticmethod
    def _request_id(headers: httpx.Headers) -> str | None:
        for name in _REQUEST_ID_HEADERS:
            value = headers.get(name)
            if value:
                return value
        return None

    @staticmethod
    def _is_replay(headers: httpx.Headers) -> bool:
        return headers.get("idempotent-replay", "").strip().lower() == "true"

    def _backoff(self, attempt: int) -> float:
        return random.uniform(0.0, min(self.retry_max_delay, self.retry_base_delay * (2**attempt)))

    @staticmethod
    def _retry_after_seconds(headers: httpx.Headers) -> float | None:
        value = headers.get("retry-after")
        if not value:
            return None
        value = value.strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            return None
        return max(0.0, when.timestamp() - time.time())

    # ------------------------------------------------------------------ send

    async def send(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        idempotent: bool,
        idempotency_key: str | None = None,
        timeout: httpx.Timeout | None = None,
        replay_needs_key: bool = False,
    ) -> RawResponse:
        """Send one logical request with retries and return the final response.

        The same ``content`` bytes are sent on every attempt. Retry decisions
        follow the SDK policy: idempotent requests retry on transport errors,
        429, 5xx and 409; non-idempotent ones only when the request provably
        never reached the server, and raise
        :class:`~whire.exceptions.AmbiguousResponseError` otherwise. A stored
        replay (``idempotent-replay: true``) is returned without retrying.

        ``replay_needs_key`` marks a request that is only safe to repeat because
        of its ``Idempotency-Key``: when its retries are exhausted on an
        ambiguous transport error it raises
        :class:`~whire.exceptions.AmbiguousResponseError` telling the caller to
        repeat the call with the same key.
        """
        self._check_open()
        self._warn_if_unauthenticated()
        request_headers = dict(self._auth_headers())
        if headers:
            request_headers.update(headers)
        if content is not None:
            request_headers.setdefault("Content-Type", "application/json")
        if idempotency_key is not None:
            request_headers["Idempotency-Key"] = idempotency_key
        if "?" in path or "#" in path or any(seg in (".", "..") for seg in path.split("/")):
            raise InvalidInputError("path must not contain a query, a fragment or dot segments")
        url = self.base_url + path
        params = {k: v for k, v in (query or {}).items() if v is not None} or None
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = await self._http.request(
                    method,
                    url,
                    content=content,
                    params=params,
                    headers=request_headers,
                    timeout=timeout or self.default_timeout,
                )
            except httpx.HTTPError as exc:
                error = self._map_transport_error(exc, idempotent, idempotency_key)
                self._log(method, path, None, attempt, started, idempotency_key, None, str(error))
                if error.is_retryable and attempt < self.max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                if replay_needs_key and idempotency_key is not None and isinstance(exc, _AMBIGUOUS_TRANSPORT_ERRORS):
                    # Retries are exhausted and the last attempt may have reached the server.
                    raise AmbiguousResponseError(
                        error.message,
                        idempotency_key=idempotency_key,
                        suggestion=(
                            "The request may have reached the server. Repeat the SAME call with "
                            f"idempotency_key={idempotency_key!r} so the deployment replays it; "
                            "never resend without the key."
                        ),
                    ) from None
                raise error from None
            await response.aread()
            request_id = self._request_id(response.headers)
            replayed = self._is_replay(response.headers)
            self._log(method, path, response.status_code, attempt, started, idempotency_key, request_id, None)
            raw = RawResponse(
                status=response.status_code,
                headers=response.headers,
                body=response.content,
                request_id=request_id,
                replayed=replayed,
                idempotency_key=idempotency_key,
                attempts=attempt + 1,
            )
            if raw.status < 400 or replayed or raw.status not in _RETRY_STATUSES:
                return raw
            if not idempotent and raw.status in _AMBIGUOUS_STATUSES:
                raise AmbiguousResponseError(
                    f"HTTP {raw.status} after the request may have reached the server",
                    status_code=raw.status,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                )
            if not idempotent and raw.status not in _SAFE_STATUSES:
                return raw
            if attempt >= self.max_retries:
                return raw
            delay = self._retry_delay(raw, attempt)
            if delay is None:
                return raw  # Retry-After beyond the cap: the caller maps the status
            await asyncio.sleep(delay)
            attempt += 1

    def _retry_delay(self, raw: RawResponse, attempt: int) -> float | None:
        retry_after = self._retry_after_seconds(raw.headers)
        if retry_after is None:
            return self._backoff(attempt)
        if retry_after > self.retry_max_delay:
            return None
        return retry_after

    def _map_transport_error(
        self, exc: httpx.HTTPError, idempotent: bool, idempotency_key: str | None
    ) -> NetworkError:
        text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        if isinstance(exc, _SAFE_TRANSPORT_ERRORS):
            return NetworkError(text, idempotency_key=idempotency_key)
        if isinstance(exc, _AMBIGUOUS_TRANSPORT_ERRORS):
            if idempotent:
                return NetworkError(text, idempotency_key=idempotency_key)
            return AmbiguousResponseError(text, idempotency_key=idempotency_key)
        error = NetworkError(text, idempotency_key=idempotency_key)
        error._retryable = False
        return error

    @staticmethod
    def _log(
        method: str,
        path: str,
        status: int | None,
        attempt: int,
        started: float,
        idempotency_key: str | None,
        request_id: str | None,
        error: str | None,
    ) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.debug(
            "whire %s %s status=%s attempt=%d elapsed_ms=%.0f idempotency_key=%s request_id=%s%s",
            method,
            path,
            status,
            attempt + 1,
            elapsed_ms,
            idempotency_key,
            request_id,
            f" error={error}" if error else "",
        )

    # ------------------------------------------------------------------ REST

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        query: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        envelope: bool = True,
        timeout_override: float | None = None,
        idempotent: bool | None = None,
    ) -> TransportResult:
        """Perform a REST call and return its unwrapped ``data``.

        Args:
            method: HTTP method.
            path: Path under the base URL (``/api/...`` or ``/x402/...``).
            json: JSON-native body (serialized once; ``None`` sends no body).
            query: Query parameters (``None`` values dropped).
            idempotency_key: Caller-supplied key; ``None`` lets the transport
                generate one for POSTs when ``auto_idempotency`` is on.
            envelope: Expect ``{"ok": ..., "data": ...}`` (``False`` for x402).
            timeout_override: Read timeout for this call (execute/settle).
            idempotent: Force the retry class; default derives from method/key/path.

        Raises:
            The mapped :class:`~whire.exceptions.WhireError` subclass for any
            failure; ``ok:false`` on a 2xx raises :class:`BadRequestError`.
        """
        content = serialize_body(json) if json is not None else None
        key = self.resolve_idempotency_key(method, path, idempotency_key)
        if idempotent is None:
            idempotent = method.upper() == "GET" or key is not None or path in _IDEMPOTENT_PATHS
        raw = await self.send(
            method,
            path,
            content=content,
            query=query,
            idempotent=idempotent,
            idempotency_key=key,
            timeout=self.timeout_with_read(timeout_override),
            replay_needs_key=key is not None and method.upper() != "GET" and path not in _IDEMPOTENT_PATHS,
        )
        return self.interpret(raw, envelope=envelope)

    def interpret(self, raw: RawResponse, *, envelope: bool = True) -> TransportResult:
        """Map a raw REST response to a result or raise the matching error."""
        try:
            body = raw.json()
        except ValueError:
            body = None
        if body is None or not isinstance(body, (dict, list)):
            self._raise_non_json(raw)
        if raw.status >= 400:
            raise self._error_for_status(raw, body)
        data = self._unwrap(raw, body, envelope)
        return TransportResult(
            data=data,
            raw_body=raw.body,
            status=raw.status,
            headers=raw.headers,
            replayed=raw.replayed,
            request_id=raw.request_id,
            idempotency_key=raw.idempotency_key,
        )

    def _raise_non_json(self, raw: RawResponse) -> None:
        text = raw.body.decode("utf-8", errors="replace").strip()[:200]
        common = {"request_id": raw.request_id, "idempotency_key": raw.idempotency_key}
        if raw.status >= 500:
            raise ServerError(text or f"HTTP {raw.status}", status_code=raw.status, **common)
        if raw.status == 401:
            raise AuthenticationError(self._auth_message(), **common)
        raise WhireError(
            text or f"HTTP {raw.status} with a non-JSON body",
            status_code=raw.status,
            error_code="invalid_response",
            **common,
        )

    def _unwrap(self, raw: RawResponse, body: Any, envelope: bool) -> Any:
        if not envelope:
            return body
        if not isinstance(body, dict) or "ok" not in body:
            raise ResponseFormatError(
                "response is not an {ok, data} envelope",
                status_code=raw.status,
                payload=body,
                request_id=raw.request_id,
                idempotency_key=raw.idempotency_key,
            )
        if body.get("ok") is not True:
            raise BadRequestError(
                self._message(body),
                status_code=raw.status,
                request_id=raw.request_id,
                idempotency_key=raw.idempotency_key,
            )
        return body.get("data")

    @staticmethod
    def _message(body: Any) -> str:
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
            if isinstance(body.get("message"), str):
                return body["message"]
        return "request failed"

    def _auth_message(self) -> str:
        if self._api_key is None:
            return (
                "Authentication failed: no API key was sent. Set WHIRE_API_KEY or pass api_key= "
                "(the SDK sends it as X-API-Key by default, or Authorization: Bearer)."
            )
        header = "Authorization: Bearer" if self.auth_scheme == "bearer" else "X-API-Key"
        return (
            f"Authentication failed: the deployment rejected the key sent as {header}. "
            "Check WHIRE_API_KEY / api_key for this environment."
        )

    def _error_for_status(self, raw: RawResponse, body: Any) -> WhireError:
        message = self._message(body)
        common: dict[str, Any] = {
            "status_code": raw.status,
            "request_id": raw.request_id,
            "idempotency_key": raw.idempotency_key,
        }
        status = raw.status
        if status == 400:
            if NOT_FOUND_RE.search(message):
                return NotFoundError(message, error_code="not_found", **common)
            return BadRequestError(message, **common)
        if status == 401:
            return AuthenticationError(self._auth_message(), **common)
        if status == 404:
            return NotFoundError(message, error_code="route_not_found", **common)
        if status == 409:
            return IdempotencyConflictError(message, **common)
        if status == 422:
            return IdempotencyMismatchError(message, **common)
        if status == 429:
            return RateLimitError(message, retry_after=self._retry_after_seconds(raw.headers), **common)
        if status >= 500:
            return ServerError(message, **common)
        return WhireError(message, error_code="http_error", **common)
