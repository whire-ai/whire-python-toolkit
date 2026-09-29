"""LLM tool-calling facade over the 33 server tools (plus one SDK helper).

:class:`WhireToolkit` hands a function-calling model the tool definitions in
the format it expects (OpenAI Chat Completions, OpenAI Responses, Anthropic or
MCP) and executes the calls the model makes through :class:`~whire.WhireClient`
(REST where a route exists, MCP otherwise). Results are the service's own
objects, untouched; failures come back as dicts an agent can reason about,
never as exceptions. The two money-moving tools are gated behind a human
confirmation callback.
"""

from __future__ import annotations

import copy
import inspect
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from decimal import Decimal
from typing import Any, Literal

import httpx

from whire._validation import to_amount
from whire.client import Environment, WhireClient
from whire.exceptions import (
    AmbiguousResponseError,
    InvalidInputError,
    PayoutExecutionRefused,
    ToolError,
    WhireError,
    WhireTimeoutError,
)
from whire.models import Capabilities, PayoutStatus, WhireModel
from whire.prompts import SYSTEM_PROMPT
from whire.tools import (
    AMOUNT_PROPERTIES,
    DESTRUCTIVE_TOOLS,
    HELPER_TOOLS_BY_NAME,
    LIST_TOOL_WRAPPERS,
    TOOL_NAMES,
    TOOLS,
    TOOLS_BY_NAME,
    WAIT_FOR_PAYOUT_TOOL,
)

__all__ = ["WhireToolkit", "ToolFormat", "ConfirmCallback", "DO_NOT_RESEND_HINT"]

ToolFormat = Literal["openai", "openai-responses", "anthropic", "mcp"]
ConfirmCallback = Callable[[str, dict[str, Any], dict[str, Any]], bool | Awaitable[bool]]

_FORMATS: tuple[str, ...] = ("openai", "openai-responses", "anthropic", "mcp")
_SNAKE_RE = re.compile(r"_([a-z0-9])")
_IDEMPOTENCY_ARG = "idempotencyKey"
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,255}")

DO_NOT_RESEND_HINT = (
    "do not resend; read get_payout_status and hand the decision to a human "
    "(the payout may have been sent, may now be failed, or may be unchanged)"
)

_WAIT_DEFAULT_TIMEOUT = 30.0
_WAIT_MAX_TIMEOUT = 60.0
_IN_FLIGHT_STATUSES = frozenset({PayoutStatus.PENDING_KYC, PayoutStatus.PROCESSING})
_SIMULATION_CENT_OUTCOMES: dict[str, str] = {
    "01": "rejected: insufficient funds (AM04), payout failed",
    "02": "rejected: payee account closed (AC04), payout failed",
    "03": "unresolved execute (no confirmation), then paid after the delay",
    "04": "unresolved execute (no confirmation), then failed after the delay",
    "05": "accepted, settles after the delay",
    "06": "paid, then returned by the counterparty bank (AC01) after the delay",
    "07": "rail unavailable, nothing sent, payout failed",
}


def _snake_to_camel(name: str) -> str:
    return _SNAKE_RE.sub(lambda match: match.group(1).upper(), name)


class WhireToolkit:
    """Tool definitions and execution for LLM agents.

    Build it around an existing :class:`~whire.WhireClient` (``client=``) or
    let it create one from connection settings, never both. Money-moving tools
    (``DESTRUCTIVE_TOOLS``) are executed only after ``confirm`` approved them,
    or, without a callback, when ``allow_destructive=True``. Unless
    ``allow_destructive=True``, they stay gated even when
    ``require_confirmation`` omits them.

    Args:
        client: A client to use; ``close()`` then leaves it open.
        api_key, environment, base_url, timeout, max_retries, auth_scheme, transport:
            Connection settings for a client the toolkit creates and owns.
        allow_destructive: Execute destructive tools without a ``confirm`` callback.
        confirm: ``confirm(tool_name, arguments, summary) -> bool`` (sync or
            async), asked before every tool in ``require_confirmation``.
        require_confirmation: Tools that need a human decision (default: the
            destructive ones). A collection of tool names, not a string.
    """

    def __init__(
        self,
        client: WhireClient | None = None,
        *,
        api_key: str | None = None,
        environment: Environment | str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = 30.0,
        max_retries: int = 3,
        auth_scheme: Literal["x-api-key", "bearer"] = "x-api-key",
        transport: httpx.AsyncBaseTransport | None = None,
        allow_destructive: bool = False,
        confirm: ConfirmCallback | None = None,
        require_confirmation: Collection[str] = DESTRUCTIVE_TOOLS,
    ) -> None:
        connection_given = (
            api_key is not None
            or environment is not None
            or base_url is not None
            or timeout != 30.0
            or max_retries != 3
            or auth_scheme != "x-api-key"
            or transport is not None
        )
        if client is not None and connection_given:
            raise TypeError("pass either client= or connection settings, not both")
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = WhireClient(
                api_key,
                environment=environment,
                base_url=base_url,
                timeout=timeout,
                max_retries=max_retries,
                auth_scheme=auth_scheme,
                transport=transport,
            )
            self._owns_client = True
        if confirm is not None and not callable(confirm):
            raise TypeError("confirm must be callable")
        self.allow_destructive = bool(allow_destructive)
        self.confirm = confirm
        if isinstance(require_confirmation, (str, bytes)):
            raise TypeError("require_confirmation must be a collection of tool names, not a string")
        names = frozenset(require_confirmation)
        unknown = names - set(TOOL_NAMES) - set(HELPER_TOOLS_BY_NAME)
        if unknown:
            raise ValueError(f"require_confirmation names unknown tool(s): {sorted(unknown)}")
        self.require_confirmation: frozenset[str] = names
        self._capabilities: Capabilities | None = None
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = self._build_handlers()

    # ------------------------------------------------------------------ properties

    @property
    def client(self) -> WhireClient:
        """The underlying client (owned by the toolkit unless ``client=`` was passed)."""
        return self._client

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The 33 server tool names, in the server's order."""
        return TOOL_NAMES

    @property
    def destructive_tools(self) -> frozenset[str]:
        """Tools that move money (``destructiveHint: true``)."""
        return DESTRUCTIVE_TOOLS

    @property
    def system_prompt(self) -> str:
        """Agent guidance consistent with the service's documented behaviour."""
        return SYSTEM_PROMPT

    @property
    def capabilities(self) -> Capabilities | None:
        """The last ``get_capabilities`` result executed through this toolkit, if any."""
        return self._capabilities

    def __repr__(self) -> str:
        return (
            f"WhireToolkit(base_url={self._client.base_url!r}, "
            f"api_key={self._client._transport.masked_api_key!r}, "
            f"allow_destructive={self.allow_destructive!r}, confirm={'set' if self.confirm else None!r})"
        )

    # ------------------------------------------------------------------ lifecycle

    async def close(self) -> None:
        """Close the client when the toolkit created it (idempotent)."""
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> WhireToolkit:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ tool definitions

    def get_tools(
        self,
        format: ToolFormat = "openai",
        *,
        names: Collection[str] | None = None,
        exclude_destructive: bool = False,
        include_helpers: bool = False,
    ) -> list[dict[str, Any]]:
        """Tool definitions for a function-calling API.

        ``openai`` (Chat Completions), ``openai-responses`` (flat) and
        ``anthropic`` carry the input schema without its top-level ``$schema``;
        ``mcp`` returns the server's own shape (``inputSchema``,
        ``outputSchema``, ``annotations``, ``execution``) verbatim. Everything is
        deep-copied; ``TOOLS`` is never mutated.
        """
        if format not in _FORMATS:
            raise ValueError(f"format must be one of {list(_FORMATS)}, got {format!r}")
        selected: list[dict[str, Any]] = list(TOOLS)
        if include_helpers:
            selected.append(WAIT_FOR_PAYOUT_TOOL)
        if names is not None:
            wanted = set(names)
            known = {tool["name"] for tool in selected}
            unknown = wanted - known
            if unknown:
                raise ValueError(f"unknown tool(s): {sorted(unknown)}")
            selected = [tool for tool in selected if tool["name"] in wanted]
        if exclude_destructive:
            selected = [tool for tool in selected if tool["name"] not in DESTRUCTIVE_TOOLS]
        return [self._render(tool, format) for tool in selected]

    @staticmethod
    def _render(tool: dict[str, Any], format: str) -> dict[str, Any]:
        if format == "mcp":
            rendered: dict[str, Any] = {
                "name": tool["name"],
                "title": tool["title"],
                "description": tool["description"],
                "inputSchema": copy.deepcopy(tool["input_schema"]),
            }
            if tool.get("output_schema") is not None:
                rendered["outputSchema"] = copy.deepcopy(tool["output_schema"])
            if tool.get("annotations") is not None:
                rendered["annotations"] = copy.deepcopy(tool["annotations"])
            if tool.get("execution") is not None:
                rendered["execution"] = copy.deepcopy(tool["execution"])
            return rendered
        parameters = copy.deepcopy(tool["input_schema"])
        parameters.pop("$schema", None)
        if format == "openai":
            return {
                "type": "function",
                "function": {"name": tool["name"], "description": tool["description"], "parameters": parameters},
            }
        if format == "openai-responses":
            return {"type": "function", "name": tool["name"], "description": tool["description"], "parameters": parameters}
        return {"name": tool["name"], "description": tool["description"], "input_schema": parameters}

    # ------------------------------------------------------------------ execution

    async def execute(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one tool call and return the service's result or an error dict.

        Steps: unknown tool → error dict; normalise arguments (snake_case →
        camelCase, numeric strings → numbers, amounts validated); ask for
        confirmation when the tool needs one; dispatch to the client; map any
        :class:`~whire.WhireError` to ``e.to_agent_dict()``. Never raises for a
        ``WhireError``.
        """
        definition = TOOLS_BY_NAME.get(name) or HELPER_TOOLS_BY_NAME.get(name)
        if definition is None:
            return ToolError(
                f"Unknown tool: {name}",
                tool_name=str(name),
                error_code="unknown_tool",
                suggestion="Use one of the tool names returned by get_tools().",
            ).to_agent_dict()
        try:
            normalised = self._normalise(definition, arguments)
        except WhireError as exc:
            return exc.to_agent_dict()
        if name in self.require_confirmation or (name in DESTRUCTIVE_TOOLS and not self.allow_destructive):
            gate = await self._gate(name, normalised)
            if gate is not None:
                return gate
        try:
            return await self._handlers[name](normalised)
        except WhireError as exc:
            return self._error_dict(exc)

    # --- argument normalisation

    @staticmethod
    def _normalise(definition: dict[str, Any], arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise InvalidInputError(f"arguments must be an object, not {type(arguments).__name__}")
        schema = definition["input_schema"]
        properties: dict[str, Any] = schema.get("properties") or {}
        result: dict[str, Any] = {}
        for key, value in arguments.items():
            key = str(key)
            if key not in properties and "_" in key:
                camel = _snake_to_camel(key)
                if (camel in properties or camel == _IDEMPOTENCY_ARG) and camel not in arguments:
                    key = camel
            result[key] = value
        key_value = result.get(_IDEMPOTENCY_ARG)
        if key_value is not None and (
            not isinstance(key_value, str) or not _IDEMPOTENCY_KEY_RE.fullmatch(key_value)
        ):
            raise InvalidInputError(
                f"{_IDEMPOTENCY_ARG} must be a string of letters, digits, '.', '_', ':' or '-' (1-255 characters)"
            )
        for key, spec in properties.items():
            if key not in result or not isinstance(spec, dict):
                continue
            value = result[key]
            if key in AMOUNT_PROPERTIES or spec.get("type") == "number":
                result[key] = _coerce_number(key, value, amount=key in AMOUNT_PROPERTIES)
        missing = [key for key in schema.get("required", []) if key not in result or result[key] is None]
        if missing:
            raise InvalidInputError(f"missing required argument(s): {', '.join(missing)}")
        return result

    # --- confirmation gating

    async def _gate(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if self.confirm is None:
            if self.allow_destructive:
                return None
            return {
                "error": f"{name} moves money and requires human confirmation",
                "error_code": "confirmation_required",
                "status_code": None,
                "retryable": False,
                "needs_user_action": True,
                "is_input_error": False,
                "suggestion": (
                    f"Ask the user to approve {name} with these exact arguments; the operator must configure a "
                    "confirm callback (or allow_destructive=True) before it can run."
                ),
                "request_id": None,
                "idempotency_key": None,
            }
        try:
            summary = await self._summary(name, arguments)
        except WhireError as exc:
            return self._error_dict(exc)
        decision = self.confirm(name, dict(arguments), summary)
        if inspect.isawaitable(decision):
            decision = await decision
        if decision is True:
            return None
        return {
            "error": f"Confirmation declined for {name}",
            "error_code": "confirmation_declined",
            "status_code": None,
            "retryable": False,
            "needs_user_action": True,
            "is_input_error": False,
            "suggestion": "The human did not approve this action; do not retry it or work around it.",
            "request_id": None,
            "idempotency_key": None,
            "summary": summary,
        }

    async def _summary(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "execute_payout":
            payout = await self._client.payouts.get(str(arguments["payoutId"]))
            raw = payout.raw or {}
            counterparty_iban: str | None = None
            counterparty_name: str | None = None
            try:
                beneficiary = await self._client.beneficiaries.get(payout.beneficiary_id)
            except WhireError:
                pass
            else:
                counterparty_iban = beneficiary.iban
                counterparty_name = beneficiary.full_name
            return {
                "payoutId": raw.get("payoutId", payout.payout_id),
                "amount": raw.get("amount"),
                "currency": raw.get("currency", payout.currency),
                "status": raw.get("status", str(payout.status)),
                "beneficiaryId": raw.get("beneficiaryId", payout.beneficiary_id),
                "counterpartyIban": counterparty_iban,
                "counterpartyName": counterparty_name,
                "mandateId": raw.get("mandateId"),
            }
        if name == "pay_x402_resource":
            quote = await self._client.x402.quote(url=str(arguments["url"]))
            requirement = (quote.raw or {}).get("requirement") or {}
            extra = requirement.get("extra") or {}
            return {
                "url": quote.url,
                "amount": requirement.get("amount"),
                "asset": requirement.get("asset"),
                "payTo": requirement.get("payTo"),
                "payeeName": extra.get("payeeName"),
                "mandateId": arguments.get("mandateId"),
            }
        return {key: _jsonable(value) for key, value in arguments.items()}

    # --- error mapping

    @staticmethod
    def _error_dict(exc: WhireError) -> dict[str, Any]:
        payload = exc.to_agent_dict()
        if isinstance(exc, (PayoutExecutionRefused, AmbiguousResponseError)):
            payload["hint"] = DO_NOT_RESEND_HINT
        if isinstance(exc, WhireTimeoutError) and exc.last is not None:
            last = getattr(exc.last, "raw", None)
            payload["last"] = last if last is not None else _jsonable(exc.last)
        return payload

    # ------------------------------------------------------------------ dispatch table

    def _build_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
        c = self._client

        async def register_payer(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.payers.create(
                    legal_name=a["legalName"],
                    contact_first_name=a["contactFirstName"],
                    contact_last_name=a["contactLastName"],
                    email=a["email"],
                    phone=a["phone"],
                    funding_sources=_items(a["fundingSources"], "fundingSources"),
                    account_type=a.get("accountType"),
                    registration_number=a.get("registrationNumber"),
                    vat_number=a.get("vatNumber"),
                    idempotency_key=a.get(_IDEMPOTENCY_ARG),
                )
            )

        async def activate_payer_account(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.payers.activate(
                    a["payerId"], verified_by=a["verifiedBy"], note=a.get("note"), idempotency_key=a.get(_IDEMPOTENCY_ARG)
                )
            )

        async def suspend_payer_account(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.payers.suspend(a["payerId"], reason=a["reason"], idempotency_key=a.get(_IDEMPOTENCY_ARG)))

        async def add_funding_source(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.payers.add_funding_source(
                    a["payerId"],
                    type=a["type"],
                    destination=a["destination"],
                    holder_name=a.get("holderName"),
                    label=a.get("label"),
                    make_default=bool(a.get("makeDefault", False)),
                    idempotency_key=a.get(_IDEMPOTENCY_ARG),
                )
            )

        async def set_default_funding_source(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.payers.set_default_funding_source(a["payerId"], source_id=a["sourceId"]))

        async def get_payer(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.payers.get(a["payerId"]))

        async def list_payers(a: dict[str, Any]) -> dict[str, Any]:
            return {"payers": _raw_list(await c.payers.list())}

        async def authorize_payment(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.authorizations.create(
                    mandate_id=a["mandateId"],
                    amount=a["amount"],
                    currency=a.get("currency"),
                    beneficiary_id=a.get("beneficiaryId"),
                    payee=a.get("payee"),
                    payee_name=a.get("payeeName"),
                    idempotency_key=a.get(_IDEMPOTENCY_ARG),
                )
            )

        async def verify_authorization_receipt(a: dict[str, Any]) -> dict[str, Any]:
            receipt = a["receipt"]
            if not isinstance(receipt, Mapping):
                raise InvalidInputError("receipt must be the receipt object exactly as authorize_payment returned it")
            return _raw(await c.authorizations.verify(receipt, idempotency_key=a.get(_IDEMPOTENCY_ARG)))

        async def get_capabilities(a: dict[str, Any]) -> dict[str, Any]:
            capabilities = await c.capabilities()
            self._capabilities = capabilities
            return _raw(capabilities)

        async def register_user(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.users.create(
                    first_name=a["firstName"],
                    last_name=a["lastName"],
                    email=a["email"],
                    phone=a["phone"],
                    payment_methods=_items(a["paymentMethods"], "paymentMethods"),
                )
            )

        async def add_payment_method(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.users.add_payment_method(
                    a["userId"],
                    type=a["type"],
                    destination=a["destination"],
                    holder_name=a.get("holderName"),
                    label=a.get("label"),
                    make_default=bool(a.get("makeDefault", False)),
                )
            )

        async def set_default_payment_method(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.users.set_default_payment_method(a["userId"], method_id=a["methodId"]))

        async def get_user(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.users.get(a["userId"]))

        async def list_users(a: dict[str, Any]) -> dict[str, Any]:
            return {"users": _raw_list(await c.users.list())}

        async def create_beneficiary_for_user(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.users.create_beneficiary(a["userId"], method_id=a.get("methodId")))

        async def validate_iban(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.beneficiaries.validate_iban(
                    iban=a["iban"], country_code=a.get("countryCode"), idempotency_key=a.get(_IDEMPOTENCY_ARG)
                )
            )

        async def create_beneficiary(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.beneficiaries.create(
                    full_name=a["fullName"],
                    iban=a["iban"],
                    reference=a["reference"],
                    email=a.get("email"),
                    wallet_address=a.get("walletAddress"),
                    idempotency_key=a.get(_IDEMPOTENCY_ARG),
                )
            )

        async def get_beneficiary(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.beneficiaries.get(a["beneficiaryId"]))

        async def create_payment_mandate(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.mandates.create(
                    beneficiary_id=a["beneficiaryId"],
                    mandate_reference=a["mandateReference"],
                    signed_by=a["signedBy"],
                    max_amount=a["maxAmount"],
                    currency=a["currency"],
                    max_total_amount=a.get("maxTotalAmount"),
                    payer_id=a.get("payerId"),
                    funding_source_id=a.get("fundingSourceId"),
                    debtor_name=a.get("debtorName"),
                    debtor_iban=a.get("debtorIban"),
                    scheme=a.get("scheme"),
                    mandate_type=a.get("mandateType"),
                    rail=a.get("rail"),
                    valid_from=a.get("validFrom"),
                    valid_until=a.get("validUntil"),
                    signed_at=a.get("signedAt"),
                    idempotency_key=a.get(_IDEMPOTENCY_ARG),
                )
            )

        async def validate_payment_mandate(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.mandates.validate(
                    a["mandateId"],
                    amount=a.get("amount"),
                    currency=a.get("currency"),
                    beneficiary_id=a.get("beneficiaryId"),
                    payout_id=a.get("payoutId"),
                    idempotency_key=a.get(_IDEMPOTENCY_ARG),
                )
            )

        async def revoke_payment_mandate(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.mandates.revoke(a["mandateId"], reason=a.get("reason"), idempotency_key=a.get(_IDEMPOTENCY_ARG))
            )

        async def list_payment_mandates(a: dict[str, Any]) -> dict[str, Any]:
            return {"mandates": _raw_list(await c.mandates.list())}

        async def create_payout_draft(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.payouts.create(
                    beneficiary_id=a["beneficiaryId"],
                    amount=a["amount"],
                    currency=a["currency"],
                    reason=a.get("reason"),
                    mandate_id=a.get("mandateId"),
                    idempotency_key=a.get(_IDEMPOTENCY_ARG),
                )
            )

        async def submit_payout_for_review(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.payouts.submit(a["payoutId"], idempotency_key=a.get(_IDEMPOTENCY_ARG)))

        async def record_provider_event(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(
                await c.payouts.record_event(a["payoutId"], event=a["event"], idempotency_key=a.get(_IDEMPOTENCY_ARG))
            )

        async def confirm_payout(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.payouts.confirm(a["payoutId"], idempotency_key=a.get(_IDEMPOTENCY_ARG)))

        async def execute_payout(a: dict[str, Any]) -> dict[str, Any]:
            # REST, always keyed, even on a client built with auto_idempotency=False.
            key = a.get(_IDEMPOTENCY_ARG) or str(uuid.uuid4())
            return _raw(await c.payouts.execute(a["payoutId"], idempotency_key=key))

        async def get_payout_status(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.payouts.get(a["payoutId"]))

        async def list_payouts(a: dict[str, Any]) -> dict[str, Any]:
            return {"payouts": _raw_list(await c.payouts.list(status=a.get("status")))}

        async def get_simulation(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.simulation.get())

        async def quote_x402_resource(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.x402.quote(url=a["url"]))

        async def pay_x402_resource(a: dict[str, Any]) -> dict[str, Any]:
            return _raw(await c.x402.pay(url=a["url"], mandate_id=a["mandateId"], reason=a.get("reason")))

        async def wait_for_payout(a: dict[str, Any]) -> dict[str, Any]:
            timeout = a.get("timeoutSeconds")
            seconds = _WAIT_DEFAULT_TIMEOUT if timeout is None else min(float(timeout), _WAIT_MAX_TIMEOUT)
            if seconds <= 0:
                raise InvalidInputError("timeoutSeconds must be greater than zero")
            statuses = a.get("untilStatuses")
            if statuses:
                if isinstance(statuses, str) or not isinstance(statuses, Collection):
                    raise InvalidInputError("untilStatuses must be a list of payout statuses")
                payout = await c.payouts.wait(a["payoutId"], status=[str(s) for s in statuses], timeout=seconds)
            else:
                payout = await c.payouts.wait(
                    a["payoutId"], until=lambda p: p.status not in _IN_FLIGHT_STATUSES, timeout=seconds
                )
            return _raw(payout)

        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "register_payer": register_payer,
            "activate_payer_account": activate_payer_account,
            "suspend_payer_account": suspend_payer_account,
            "add_funding_source": add_funding_source,
            "set_default_funding_source": set_default_funding_source,
            "get_payer": get_payer,
            "list_payers": list_payers,
            "authorize_payment": authorize_payment,
            "verify_authorization_receipt": verify_authorization_receipt,
            "get_capabilities": get_capabilities,
            "register_user": register_user,
            "add_payment_method": add_payment_method,
            "set_default_payment_method": set_default_payment_method,
            "get_user": get_user,
            "list_users": list_users,
            "create_beneficiary_for_user": create_beneficiary_for_user,
            "validate_iban": validate_iban,
            "create_beneficiary": create_beneficiary,
            "get_beneficiary": get_beneficiary,
            "create_payment_mandate": create_payment_mandate,
            "validate_payment_mandate": validate_payment_mandate,
            "revoke_payment_mandate": revoke_payment_mandate,
            "list_payment_mandates": list_payment_mandates,
            "create_payout_draft": create_payout_draft,
            "submit_payout_for_review": submit_payout_for_review,
            "record_provider_event": record_provider_event,
            "confirm_payout": confirm_payout,
            "execute_payout": execute_payout,
            "get_payout_status": get_payout_status,
            "list_payouts": list_payouts,
            "get_simulation": get_simulation,
            "quote_x402_resource": quote_x402_resource,
            "pay_x402_resource": pay_x402_resource,
            "wait_for_payout": wait_for_payout,
        }
        missing = set(TOOL_NAMES) - set(handlers)
        if missing:  # pragma: no cover - guards a future tools_list update
            raise RuntimeError(f"no handler for tool(s): {sorted(missing)}")
        return handlers

    # ------------------------------------------------------------------ hints

    def hint(self, name: str, arguments: Mapping[str, Any] | None, result: Mapping[str, Any] | None) -> str | None:
        """Agent guidance derived from a tool result (pure; no request is made).

        Append it to the tool result you hand back to the model, e.g.
        ``{"result": result, "hint": toolkit.hint(name, args, result)}``.
        """
        if not isinstance(result, Mapping):
            return None
        arguments = arguments or {}
        if "error" in result:
            return _error_hint(result)
        status = result.get("status")
        if name == "register_payer":
            return (
                f"Payer {result.get('payerId')} is {status}: call activate_payer_account "
                "(verifiedBy = the human who checked the documents) before signing mandates."
            )
        if name == "activate_payer_account":
            return f"Payer is {status}; create_beneficiary, then create_payment_mandate with payerId={result.get('payerId')}."
        if name == "suspend_payer_account":
            return "Suspended: no new mandates. Existing mandates stay valid until revoke_payment_mandate; activate_payer_account reinstates."
        if name in ("register_user", "add_payment_method", "set_default_payment_method"):
            return "Use create_beneficiary_for_user to turn this person into a payout beneficiary."
        if name in ("create_beneficiary", "create_beneficiary_for_user"):
            return f"Sign a mandate for this payee with create_payment_mandate (beneficiaryId={result.get('beneficiaryId')})."
        if name == "validate_iban":
            return None if result.get("isValid") else f"IBAN is not valid: {result.get('explanation')}"
        if name == "create_payment_mandate":
            return (
                f"Mandate {result.get('mandateId')} is {status}; authorize_payment decides single payments, "
                "create_payout_draft with this mandateId starts a payout."
            )
        if name == "validate_payment_mandate":
            if result.get("isValid"):
                return "All ten checks passed; nothing was decided or moved."
            return f"Mandate does not cover this payment (failed: {', '.join(result.get('failedChecks') or [])}); do not create a payout for it."
        if name == "authorize_payment":
            if result.get("decision") == "approved":
                return (
                    "Approved: the receipt permits the payment; nothing was moved or reserved. "
                    f"Continue with create_payout_draft (mandateId={result.get('mandateId')})."
                )
            return f"Refused (failed: {', '.join(result.get('failedChecks') or [])}); do not create a payout for it."
        if name == "verify_authorization_receipt":
            if result.get("usable"):
                return "Receipt is usable: signed by this service, unexpired and an approval."
            return f"Receipt is not usable: {result.get('explanation')}"
        if name == "get_capabilities":
            return _capabilities_hint(result)
        if name == "get_simulation":
            if result.get("enabled") is False:
                return "No simulation on this deployment: every amount and IBAN is ordinary."
            return "Cent triggers and SIML IBANs apply only because this deployment is simulated; no money moves."
        if name == "create_payout_draft":
            return _draft_hint(result, self._capabilities)
        if name == "submit_payout_for_review":
            next_action = result.get("nextAction")
            base = next_action or "Poll get_payout_status until the payout leaves pending_kyc."
            return f"{base} Poll no more often than every 2-3 seconds."
        if name == "confirm_payout":
            return (
                f"Payout is {status}: confirmation settles nothing. execute_payout (human approval required) sends it; "
                "without it the payout stays processing."
            )
        if name == "execute_payout":
            if result.get("unresolved"):
                return "Unresolved: the rail did not confirm. Do not resend; poll get_payout_status until it leaves processing."
            if status == "paid":
                return f"Settled as {result.get('providerReference')}; nothing more to do for this payout."
            if status == "processing":
                return "Accepted; poll get_payout_status until it leaves processing. Do not resend."
            return None
        if name in ("get_payout_status", "wait_for_payout", "record_provider_event"):
            return _status_hint(status)
        if name == "quote_x402_resource":
            if result.get("free"):
                return "The resource is free: fetch it directly, no payment needed."
            requirement = result.get("requirement")
            if not isinstance(requirement, dict) or requirement.get("amount") is None:
                return (
                    f"Not payable: the URL answered HTTP {result.get('status')} without a sepa-mandate payment "
                    f"requirement (other offers: {result.get('otherOffers') or 'none'}); do not call pay_x402_resource."
                )
            extra = requirement.get("extra") or {}
            return (
                f"Costs {requirement.get('amount')} {requirement.get('asset')} to {requirement.get('payTo')} "
                f"({extra.get('payeeName') or 'payee name not given'}). pay_x402_resource moves money: get human approval first."
            )
        if name == "pay_x402_resource":
            if result.get("paid"):
                return f"Paid (payoutId={result.get('payoutId')}) and the resource was fetched (HTTP {result.get('status')})."
            return f"Not paid: {result.get('reason') or 'the seller did not accept the payment'}. Do not retry without a human."
        if name in ("list_payouts", "list_payers", "list_users", "list_payment_mandates"):
            return None
        return None


# ---------------------------------------------------------------------- helpers


def _raw(model: WhireModel) -> dict[str, Any]:
    raw = model.raw
    if raw is None:  # pragma: no cover - every parsed response carries raw
        return model.to_dict()
    return raw


def _raw_list(models: list[Any]) -> list[dict[str, Any]]:
    return [_raw(model) for model in models]


def _items(value: Any, field: str) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, Collection) or isinstance(value, (str, bytes)):
        raise InvalidInputError(f"{field} must be a list of objects")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidInputError(f"{field}[{index}] must be an object")
        items.append({_snake_to_camel(str(key)): val for key, val in item.items()})
    return items


def _coerce_number(key: str, value: Any, *, amount: bool) -> Any:
    if amount:
        return to_amount(value, field=key)  # enforces positive, <= 2 dp and the JSON-safe cap
    if isinstance(value, bool):
        raise InvalidInputError(f"{key} must be a number, not bool")
    if isinstance(value, (int, float, Decimal)):
        number = value
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise InvalidInputError(f"{key} is not a number: {value!r}") from None
    else:
        number = None
    if number is not None:
        if not math.isfinite(number):
            raise InvalidInputError(f"{key} must be a finite number")
        return number
    raise InvalidInputError(f"{key} must be a number, not {type(value).__name__}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, WhireModel):
        return value.raw if value.raw is not None else value.to_dict()
    return value


def _error_hint(result: Mapping[str, Any]) -> str | None:
    hint = result.get("hint")
    if isinstance(hint, str):
        return hint
    code = result.get("error_code")
    if code in ("confirmation_required", "confirmation_declined"):
        return "A human must approve this action; do not retry it or work around it."
    if code == "timeout":
        return "Still in flight; poll get_payout_status again later, do not repeat the action."
    suggestion = result.get("suggestion")
    return suggestion if isinstance(suggestion, str) else None


def _capabilities_hint(result: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if result.get("simulated"):
        parts.append(
            "Simulated rail: amounts ending in .01-.07 and the IBANs NL56SIML0000000001 / NL29SIML0000000002 select "
            "outcomes (see get_simulation); no money moves."
        )
    else:
        parts.append("Not simulated: every amount and IBAN is ordinary; cent-value triggers do nothing.")
    if not result.get("settlement"):
        parts.append("settlement is false: execute_payout and pay_x402_resource will be refused; authorization still works.")
    return " ".join(parts)


def _draft_hint(result: Mapping[str, Any], capabilities: Capabilities | None) -> str:
    base = f"Draft {result.get('payoutId')} created; submit_payout_for_review next."
    if capabilities is None or capabilities.simulated is not True:
        return base
    amount = result.get("amount")
    try:
        cents = f"{Decimal(str(amount)):.2f}"[-2:]
    except Exception:  # noqa: BLE001 - amount not numeric; no trigger hint
        return base
    outcome = _SIMULATION_CENT_OUTCOMES.get(cents)
    if outcome is None:
        return base
    return f"{base} On this simulated deployment an amount ending in .{cents} triggers: {outcome}."


def _status_hint(status: Any) -> str | None:
    hints = {
        "draft": "Draft: submit_payout_for_review sends it to KYC review.",
        "pending_kyc": "Waiting for the KYC decision; poll get_payout_status every 2-3 seconds until it leaves pending_kyc.",
        "kyc_rejected": "KYC rejected: terminal. A new draft would be needed; that is a human decision.",
        "approved": "Approved: execute_payout sends it (human approval required); confirm_payout is optional and settles nothing.",
        "processing": "Processing: poll get_payout_status until it leaves processing. Do not resend.",
        "paid": "Paid: settled; nothing more to do.",
        "failed": "Failed: terminal. Do not resend or create a replacement without a human.",
        "returned": "Returned: the funds came back after settlement; the amount still counts against the mandate total.",
    }
    return hints.get(str(status)) if status is not None else None
