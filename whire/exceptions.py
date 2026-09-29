"""Error hierarchy of the Whire SDK.

Every error raised by the SDK derives from :class:`WhireError`. Exceptions
never hold the ``httpx`` request or response: they carry the HTTP status, a
stable ``error_code``, the server's message sentence, the request id and the
idempotency key that was sent, and expose ``to_agent_dict()`` for tool-calling
agents.
"""

from __future__ import annotations

from typing import Any

_UNSET: Any = object()

__all__ = [
    "WhireError",
    "AuthenticationError",
    "NotFoundError",
    "BadRequestError",
    "PayoutExecutionRefused",
    "IdempotencyConflictError",
    "IdempotencyMismatchError",
    "RateLimitError",
    "ServerError",
    "NetworkError",
    "AmbiguousResponseError",
    "ResponseFormatError",
    "InvalidInputError",
    "ToolError",
    "MCPProtocolError",
    "WhireTimeoutError",
]


class WhireError(Exception):
    """Base class for every error raised by the SDK.

    Args:
        message: The server's sentence or a local description. ``str(e)`` is
            exactly this message.
        status_code: HTTP status of the failed request; omitted → the class
            default, explicit ``None`` → no HTTP status (MCP-derived errors).
        error_code: Stable machine-readable code (see each subclass).
        request_id: Server request id (``x-railway-request-id`` or similar).
        idempotency_key: The ``Idempotency-Key`` that was sent, if any.
        suggestion: Optional guidance overriding the class default.
    """

    default_error_code: str | None = None
    default_status_code: int | None = None
    _retryable: bool = False
    _needs_user_action: bool = False
    _input_error: bool = False
    _default_suggestion: str | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = _UNSET,
        error_code: str | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        suggestion: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = self.default_status_code if status_code is _UNSET else status_code
        self.error_code = error_code if error_code is not None else self.default_error_code
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self._suggestion = suggestion

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({self.message!r}, status_code={self.status_code!r}, "
            f"error_code={self.error_code!r}, request_id={self.request_id!r}, "
            f"idempotency_key={self.idempotency_key!r})"
        )

    @property
    def is_retryable(self) -> bool:
        """Whether repeating the same call may succeed."""
        return self._retryable

    @property
    def needs_user_action(self) -> bool:
        """Whether a human has to decide before anything is repeated."""
        return self._needs_user_action

    @property
    def is_input_error(self) -> bool:
        """Whether the caller's parameters caused the failure."""
        return self._input_error

    @property
    def suggestion(self) -> str | None:
        """Short guidance for the caller (never contains secrets)."""
        return self._suggestion if self._suggestion is not None else self._default_suggestion

    def to_agent_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict an LLM agent can reason about."""
        return {
            "error": self.message,
            "error_code": self.error_code,
            "status_code": self.status_code,
            "retryable": self.is_retryable,
            "needs_user_action": self.needs_user_action,
            "is_input_error": self.is_input_error,
            "suggestion": self.suggestion,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
        }


class AuthenticationError(WhireError):
    """401: the deployment rejected (or required) an API key."""

    default_error_code = "auth_failed"
    default_status_code = 401
    _needs_user_action = True
    _default_suggestion = "Set WHIRE_API_KEY (or pass api_key=) to a key accepted by this deployment."


class NotFoundError(WhireError):
    """A record (``error_code="not_found"``) or a route (``"route_not_found"``) does not exist.

    The service answers ``400 "<Kind> <id> not found."`` for unknown records and
    ``404`` only for unknown routes; both map here.
    """

    default_error_code = "not_found"
    default_status_code = 400

    @property
    def is_input_error(self) -> bool:
        return self.error_code == "not_found"

    @property
    def suggestion(self) -> str | None:
        if self._suggestion is not None:
            return self._suggestion
        if self.error_code == "route_not_found":
            return "The route does not exist on this deployment: check base_url and the SDK version."
        return "Check the id; list the records to find the right one."


class BadRequestError(WhireError):
    """400: validation or business rule refused the request (also ``ok:false`` on a 2xx)."""

    default_error_code = "bad_request"
    default_status_code = 400
    _input_error = True
    _default_suggestion = "Fix the request; the message says what was refused."


class PayoutExecutionRefused(BadRequestError):
    """The rail refused, already handled, or could not process an execution.

    The payout may now be ``failed`` or unchanged: read it before deciding
    anything. Never resend or create a replacement automatically.
    """

    default_error_code = "execution_refused"
    _needs_user_action = True
    _input_error = False
    _default_suggestion = (
        "The rail refused or already handled this payout. Do not resend or create a "
        "replacement; read get_payout_status and hand the decision to a human."
    )

    def __init__(self, message: str, **kwargs: Any) -> None:
        if kwargs.get("error_code") is None and "was already executed" in message:
            kwargs["error_code"] = "already_executed"
        super().__init__(message, **kwargs)


class IdempotencyConflictError(WhireError):
    """409: the same Idempotency-Key is still being processed."""

    default_error_code = "idempotency_in_flight"
    default_status_code = 409
    _retryable = True

    @property
    def suggestion(self) -> str | None:
        if self._suggestion is not None:
            return self._suggestion
        return (
            "The first request with this key is still running; repeat the same call with "
            f"idempotency_key={self.idempotency_key!r} to receive its result, or read the "
            "record; do not create a new one."
        )


class IdempotencyMismatchError(WhireError):
    """422: the Idempotency-Key was already used with a different body."""

    default_error_code = "idempotency_mismatch"
    default_status_code = 422
    _input_error = True
    _default_suggestion = "Generate a new idempotency key for a new request; reuse a key only with identical bytes."


class RateLimitError(WhireError):
    """429: too many requests."""

    default_error_code = "rate_limited"
    default_status_code = 429
    _retryable = True
    _default_suggestion = "Wait and retry; honour retry_after when it is set."

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return super().__repr__()[:-1] + f", retry_after={self.retry_after!r})"


class ServerError(WhireError):
    """5xx: the deployment failed."""

    default_error_code = "server_error"
    default_status_code = 500
    _retryable = True
    _default_suggestion = "Retry with backoff; if it persists, the deployment is unhealthy."


class NetworkError(WhireError):
    """The request never produced an HTTP response (connection or timeout)."""

    default_error_code = "network_error"
    _retryable = True
    _default_suggestion = "Check connectivity and retry."


class AmbiguousResponseError(NetworkError):
    """A non-idempotent request failed after it may have reached the server."""

    default_error_code = "ambiguous_outcome"
    _retryable = False
    _needs_user_action = True
    _default_suggestion = (
        "The request may have been processed. Read the current state "
        "(get_payout_status / list_payouts / get_user) before repeating it."
    )


class ResponseFormatError(WhireError):
    """The server answered 2xx with a body the SDK cannot interpret."""

    default_error_code = "invalid_response"
    _default_suggestion = "Upgrade the SDK or check base_url; the response shape is not the one documented."

    def __init__(self, message: str, *, payload: Any = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.payload = payload


class InvalidInputError(WhireError):
    """Local validation failed before any request was sent."""

    default_error_code = "invalid_input"
    _input_error = True
    _default_suggestion = "Fix the parameter named in the message; nothing was sent."


class ToolError(WhireError):
    """An MCP ``tools/call`` answered ``isError: true``.

    ``error_code`` is ``"unknown_tool"``, ``"invalid_arguments"`` or ``"tool_error"``.
    """

    default_error_code = "tool_error"
    _input_error = True

    def __init__(self, message: str, *, tool_name: str | None = None, data: Any = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.tool_name = tool_name
        self.data = data

    def __repr__(self) -> str:
        return super().__repr__()[:-1] + f", tool_name={self.tool_name!r})"

    @property
    def suggestion(self) -> str | None:
        if self._suggestion is not None:
            return self._suggestion
        if self.error_code == "unknown_tool":
            return "Call tools/list and use one of the tool names it returns."
        if self.error_code == "invalid_arguments":
            return "Fix the arguments to match the tool's inputSchema (camelCase names, numbers for amounts)."
        return "The tool refused the request; the message says why."


class MCPProtocolError(WhireError):
    """The MCP endpoint answered a JSON-RPC error or an HTTP error."""

    default_error_code = "mcp_error"

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: Any = None,
        input_error: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.code = code
        self.data = data
        self._input_error = input_error

    def __repr__(self) -> str:
        return super().__repr__()[:-1] + f", code={self.code!r})"


class WhireTimeoutError(WhireError, TimeoutError):
    """A polling helper ran out of time.

    Attributes:
        payout_id: The payout being polled.
        last: The last :class:`~whire.models.Payout` read, or ``None``.
        elapsed: Seconds spent polling.
    """

    default_error_code = "timeout"
    _default_suggestion = "The record is still in flight; poll again later, do not repeat the action."

    def __init__(
        self,
        message: str,
        *,
        payout_id: str | None = None,
        last: Any = None,
        elapsed: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.payout_id = payout_id
        self.last = last
        self.elapsed = elapsed
