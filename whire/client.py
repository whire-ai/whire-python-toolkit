"""The async client for the agent-payouts API."""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Literal

import httpx

from whire import __version__
from whire._resources import (
    AuthorizationsAPI,
    BeneficiariesAPI,
    MandatesAPI,
    PayersAPI,
    PayoutsAPI,
    SimulationAPI,
    UsersAPI,
    X402API,
)
from whire._transport import HttpTransport
from whire._validation import _check_production_key, _validate_base_url
from whire.exceptions import InvalidInputError
from whire.mcp_client import WhireMCPClient, build_timeout, default_user_agent
from whire.models import Capabilities, Health

__all__ = ["Environment", "WhireClient"]

logger = logging.getLogger("whire")


class Environment(Enum):
    """Known deployments. Any other deployment is reached through ``base_url``."""

    PRODUCTION = "https://api.whire.ai"
    SANDBOX = "https://sandbox.whire.ai"

    @classmethod
    def parse(cls, value: Environment | str) -> Environment:
        """``"sandbox"`` / ``"production"`` (case-insensitive) or a member; URLs are refused."""
        if isinstance(value, Environment):
            return value
        name = str(value).strip().upper()
        if name in cls.__members__:
            return cls[name]
        raise InvalidInputError(
            f"environment must be 'sandbox' or 'production', got {value!r}; pass base_url= for a custom deployment"
        )


class WhireClient:
    """Async client covering the whole REST surface plus the MCP-only operations.

    One ``httpx.AsyncClient`` is created in ``__init__`` and shared by every
    namespace and by ``client.mcp``. The client is safe for concurrent use from
    tasks on one event loop; do not share it across loops or threads.

    Args:
        api_key: API key (else ``WHIRE_API_KEY``); blank means none.
        environment: ``"sandbox"`` / ``"production"`` or an :class:`Environment`.
        base_url: Explicit deployment origin; wins over ``environment``.
        auth_scheme: ``"x-api-key"`` (default) or ``"bearer"``.
        timeout: Read timeout in seconds, or an ``httpx.Timeout``.
        execute_timeout: Read timeout for execute / settle / pay calls.
        max_retries: Extra attempts after the first (0–10).
        retry_base_delay: Base of the exponential backoff, seconds.
        retry_max_delay: Cap for backoff and honoured ``Retry-After``.
        auto_idempotency: Send a fresh ``Idempotency-Key`` on every POST.
        allow_unauthenticated: Allow a production client without a key.
        allow_insecure_http: Allow ``http://`` to non-local hosts.
        verify: TLS verification (``True`` or a CA bundle path).
        user_agent: Overrides ``whire-python/<v> httpx/<v>``.
        transport: An ``httpx.AsyncBaseTransport`` for tests.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        environment: Environment | str | None = None,
        base_url: str | None = None,
        auth_scheme: Literal["x-api-key", "bearer"] = "x-api-key",
        timeout: float | httpx.Timeout = 30.0,
        execute_timeout: float = 120.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 10.0,
        auto_idempotency: bool = True,
        allow_unauthenticated: bool = False,
        allow_insecure_http: bool = False,
        verify: bool | str = True,
        user_agent: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._environment = _resolve_environment(environment, base_url)
        self._base_url = _resolve_base_url(environment, base_url, allow_insecure_http)
        key = _resolve_api_key(api_key)
        _check_production_key(self._base_url, key, allow_unauthenticated)
        _check_settings(auth_scheme, max_retries, retry_base_delay, retry_max_delay, execute_timeout, user_agent)
        logger.info("whire: using base URL %s", self._base_url)
        self.execute_timeout = float(execute_timeout)
        default_timeout = build_timeout(timeout)
        self._http = httpx.AsyncClient(
            transport=transport,
            verify=verify,
            timeout=default_timeout,
            headers={"User-Agent": user_agent or default_user_agent()},
        )
        self._transport = HttpTransport(
            self._http,
            base_url=self._base_url,
            api_key=key,
            auth_scheme=auth_scheme,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            auto_idempotency=auto_idempotency,
            default_timeout=default_timeout,
            owns_http=True,
        )
        self.mcp = WhireMCPClient(
            self._base_url, execute_timeout=self.execute_timeout, http_transport=self._transport
        )
        self.payers = PayersAPI(self)
        self.users = UsersAPI(self)
        self.beneficiaries = BeneficiariesAPI(self)
        self.mandates = MandatesAPI(self)
        self.authorizations = AuthorizationsAPI(self)
        self.payouts = PayoutsAPI(self)
        self.simulation = SimulationAPI(self)
        self.x402 = X402API(self)

    # ------------------------------------------------------------------ properties

    @property
    def base_url(self) -> str:
        """The deployment origin every request is sent to."""
        return self._base_url

    @property
    def environment(self) -> Environment | None:
        """The known environment, or ``None`` for a custom ``base_url``."""
        return self._environment

    @property
    def is_closed(self) -> bool:
        return self._transport.closed

    def __repr__(self) -> str:
        return f"WhireClient(base_url={self._base_url!r}, api_key={self._transport.masked_api_key!r})"

    # ------------------------------------------------------------------ lifecycle

    async def close(self) -> None:
        """Close the HTTP client (idempotent); later requests raise ``client_closed``."""
        await self._transport.close()

    async def __aenter__(self) -> WhireClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ service

    async def health(self) -> Health:
        """Liveness (``GET /api/health``; no key needed)."""
        result = await self._transport.request("GET", "/api/health")
        return Health.from_wire(result.data, replayed=result.replayed)

    async def capabilities(self) -> Capabilities:
        """What this deployment can do (``GET /api/capabilities``; no key needed)."""
        result = await self._transport.request("GET", "/api/capabilities")
        return Capabilities.from_wire(result.data, replayed=result.replayed)


# ---------------------------------------------------------------------- resolution helpers


def _resolve_environment(environment: Environment | str | None, base_url: str | None) -> Environment | None:
    if base_url is not None:
        return _environment_for_url(base_url)
    if environment is not None:
        return Environment.parse(environment)
    if os.environ.get("WHIRE_BASE_URL", "").strip():
        return _environment_for_url(os.environ["WHIRE_BASE_URL"])
    env_name = os.environ.get("WHIRE_ENVIRONMENT", "").strip()
    return Environment.parse(env_name) if env_name else Environment.SANDBOX


def _environment_for_url(url: str) -> Environment | None:
    normalized = url.strip().rstrip("/")
    for member in Environment:
        if member.value == normalized:
            return member
    return None


def _resolve_base_url(environment: Environment | str | None, base_url: str | None, allow_insecure_http: bool) -> str:
    if base_url is None and environment is None:
        base_url = os.environ.get("WHIRE_BASE_URL", "").strip() or None
    if base_url is None:
        resolved = _resolve_environment(environment, None)
        return (resolved or Environment.SANDBOX).value
    return _validate_base_url(base_url, allow_insecure_http)


def _resolve_api_key(api_key: str | None) -> str | None:
    if api_key is None:
        api_key = os.environ.get("WHIRE_API_KEY")
    if api_key is None:
        return None
    key = api_key.strip()
    return key or None


def _check_settings(
    auth_scheme: str,
    max_retries: int,
    retry_base_delay: float,
    retry_max_delay: float,
    execute_timeout: float,
    user_agent: str | None,
) -> None:
    if auth_scheme not in ("x-api-key", "bearer"):
        raise InvalidInputError("auth_scheme must be 'x-api-key' or 'bearer'")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 10:
        raise InvalidInputError("max_retries must be an integer between 0 and 10")
    if retry_base_delay < 0 or retry_max_delay < 0:
        raise InvalidInputError("retry delays must not be negative")
    if execute_timeout <= 0:
        raise InvalidInputError("execute_timeout must be positive")
    if user_agent is not None and not user_agent.strip():
        raise InvalidInputError("user_agent must not be blank")
