"""Resource namespaces of :class:`~whire.client.WhireClient`.

Each namespace maps its methods onto REST routes (``/api/...``, ``/x402/...``)
or, where no route exists, onto MCP tools served by ``/mcp``. All parameters
are keyword-only except the leading resource id.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote, urlsplit

from whire._transport import NOT_FOUND_RE, TransportResult
from whire._validation import (
    MAX_MANDATE_DURATION_DAYS,
    amount_to_json,
    enum_value,
    is_production_host,
    require_id,
    require_str,
    to_amount,
    to_iso_datetime,
    upper_currency,
    validate_mandate_reference,
    validated_enum,
)
from whire.exceptions import (
    BadRequestError,
    InvalidInputError,
    MCPProtocolError,
    NotFoundError,
    PayoutExecutionRefused,
    ToolError,
    WhireError,
    WhireTimeoutError,
)
from whire.models import (
    AccountType,
    Amount,
    AuthorizationReceipt,
    Beneficiary,
    Capabilities,
    FundingSourceInput,
    FundingSourceType,
    Health,
    IbanValidation,
    Mandate,
    MandateScheme,
    MandateStatus,
    MandateType,
    MandateValidation,
    Payer,
    PayerStatus,
    PaymentMethodInput,
    Payout,
    PayoutExecution,
    PayoutStatus,
    PayoutSubmission,
    PayoutTransition,
    ProviderEvent,
    Rail,
    ReceiptVerification,
    ResetResult,
    Simulation,
    User,
    WhireModel,
    X402Payment,
    X402PaymentPayload,
    X402PaymentRequirements,
    X402Quote,
    X402SettleResult,
    X402Supported,
    X402VerifyResult,
)

if TYPE_CHECKING:
    from whire.client import WhireClient

__all__ = [
    "PayersAPI",
    "UsersAPI",
    "BeneficiariesAPI",
    "MandatesAPI",
    "AuthorizationsAPI",
    "PayoutsAPI",
    "SimulationAPI",
    "X402API",
]

M = TypeVar("M", bound=WhireModel)
DateLike = datetime | date | str


# ---------------------------------------------------------------------- shared helpers


def _path_id(value: Any, *, field: str) -> str:
    """Validate a record id and percent-encode it for use as one URL path segment."""
    return quote(require_id(value, field=field), safe=":")


def _body(**fields: Any) -> dict[str, Any]:
    """Drop ``None`` values so optional fields are omitted from the wire."""
    return {key: value for key, value in fields.items() if value is not None}


def _parse(model: type[M], result: TransportResult) -> M:
    return model.from_wire(result.data, replayed=result.replayed)


def _parse_list(model: type[M], result: TransportResult, key: str) -> list[M]:
    data = result.data
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        from whire.exceptions import ResponseFormatError

        raise ResponseFormatError(f"expected a {key!r} list in the response", payload=data)
    return [model.from_wire(item, replayed=result.replayed) for item in items]


def _translate_mcp_error(error: WhireError) -> WhireError:
    """Map MCP-level errors onto the REST-style hierarchy (see SPEC §3.1)."""
    common: dict[str, Any] = {
        "status_code": None,
        "request_id": error.request_id,
        "idempotency_key": error.idempotency_key,
    }
    if isinstance(error, ToolError):
        # Argument / unknown-tool failures first: their text may end in "not found" too
        # ("MCP error -32602: Tool nope not found") but they are the caller's mistake, not a missing record.
        if error.error_code in ("invalid_arguments", "unknown_tool") or error.message.startswith("MCP error -32602"):
            return BadRequestError(error.message, error_code="invalid_arguments", **common)
        if NOT_FOUND_RE.search(error.message):
            return NotFoundError(error.message, error_code="not_found", **common)
        return BadRequestError(error.message, **common)
    if isinstance(error, MCPProtocolError):
        # Only a -32603 business sentence ("Payout nope not found.") means a missing record; other protocol
        # errors (-32601 "Method not found", ...) are transport-level failures.
        if error.code == -32603 and NOT_FOUND_RE.search(error.message):
            return NotFoundError(error.message, error_code="not_found", **common)
        if error.is_input_error:
            return BadRequestError(error.message, error_code="invalid_arguments", **common)
        return WhireError(error.message, error_code="mcp_error", **{**common, "status_code": error.status_code})
    return error


def _model_payload(value: WhireModel | Mapping[str, Any], *, field: str) -> dict[str, Any]:
    """The wire object for a model or mapping (a model's ``raw`` wins; else a JSON dump)."""
    if isinstance(value, WhireModel):
        if value.raw is not None:
            return value.raw
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    raise InvalidInputError(f"{field} must be a model or a mapping")


def _inputs(items: Sequence[WhireModel | Mapping[str, Any]], model: type[M], *, field: str) -> list[dict[str, Any]]:
    """Validate a sequence of input models/dicts and dump them with wire names."""
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        raise InvalidInputError(f"{field} must be a non-empty sequence")
    dumped: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if isinstance(item, Mapping):
            try:
                item = model.model_validate(dict(item))
            except Exception as exc:  # pydantic ValidationError
                raise InvalidInputError(f"{field}[{index}] is invalid: {exc}") from None
        elif not isinstance(item, model):
            raise InvalidInputError(f"{field}[{index}] must be a {model.__name__} or a dict")
        dumped.append(_dump_input(item))
    return dumped


def _dump_input(item: WhireModel) -> dict[str, Any]:
    data = item.model_dump(mode="json", by_alias=True, exclude_none=True)
    if data.get("makeDefault") is False:
        del data["makeDefault"]  # the docs' bodies omit it; the server defaults to false
    for key, value in list(data.items()):
        if isinstance(value, Decimal):
            data[key] = amount_to_json(value)
    return data


class _Namespace:
    def __init__(self, client: WhireClient) -> None:
        self._client = client

    @property
    def _transport(self) -> Any:
        return self._client._transport

    async def _rest(self, model: type[M], method: str, path: str, **kwargs: Any) -> M:
        return _parse(model, await self._transport.request(method, path, **kwargs))

    async def _tool(self, model: type[M], name: str, arguments: Mapping[str, Any]) -> M:
        try:
            payload = await self._client.mcp.call_tool(name, arguments)
        except (ToolError, MCPProtocolError) as exc:
            raise _translate_mcp_error(exc) from None
        return model.from_wire(payload)


# ---------------------------------------------------------------------- payers


class PayersAPI(_Namespace):
    """Payers: the customers whose money moves."""

    async def create(
        self,
        *,
        legal_name: str,
        contact_first_name: str,
        contact_last_name: str,
        email: str,
        phone: str,
        funding_sources: Sequence[FundingSourceInput | Mapping[str, Any]],
        account_type: AccountType | str | None = None,
        registration_number: str | None = None,
        vat_number: str | None = None,
        idempotency_key: str | None = None,
    ) -> Payer:
        """Sign up a payer (``POST /api/payers``); it opens ``pending_verification``.

        A funding-source IBAN belongs to at most one payer and an email may be
        registered once (the server refuses duplicates with a 400).
        """
        body = _body(
            legalName=require_str(legal_name, field="legal_name"),
            accountType=enum_value(account_type),
            registrationNumber=registration_number,
            vatNumber=vat_number,
            contactFirstName=require_str(contact_first_name, field="contact_first_name"),
            contactLastName=require_str(contact_last_name, field="contact_last_name"),
            email=require_str(email, field="email"),
            phone=require_str(phone, field="phone"),
            fundingSources=_inputs(funding_sources, FundingSourceInput, field="funding_sources"),
        )
        return await self._rest(Payer, "POST", "/api/payers", json=body, idempotency_key=idempotency_key)

    async def activate(
        self, payer_id: str, /, *, verified_by: str, note: str | None = None, idempotency_key: str | None = None
    ) -> Payer:
        """Record the verification decision (``POST /api/payers/{id}/activate``).

        Also reinstates a ``suspended`` payer.
        """
        payer_id = _path_id(payer_id, field="payer_id")
        body = _body(verifiedBy=require_str(verified_by, field="verified_by"), note=note)
        return await self._rest(
            Payer, "POST", f"/api/payers/{payer_id}/activate", json=body, idempotency_key=idempotency_key
        )

    async def suspend(self, payer_id: str, /, *, reason: str, idempotency_key: str | None = None) -> Payer:
        """Suspend a payer: no new mandates; existing ones stay until revoked."""
        payer_id = _path_id(payer_id, field="payer_id")
        body = {"reason": require_str(reason, field="reason")}
        return await self._rest(
            Payer, "POST", f"/api/payers/{payer_id}/suspend", json=body, idempotency_key=idempotency_key
        )

    async def add_funding_source(
        self,
        payer_id: str,
        /,
        *,
        type: FundingSourceType | str,
        destination: str,
        holder_name: str | None = None,
        label: str | None = None,
        make_default: bool = False,
        idempotency_key: str | None = None,
    ) -> Payer:
        """Add an account the payer can be debited from (``POST /api/payers/{id}/funding-sources``)."""
        payer_id = _path_id(payer_id, field="payer_id")
        body = _body(
            type=enum_value(type),
            destination=require_str(destination, field="destination"),
            holderName=holder_name,
            label=label,
            makeDefault=True if make_default else None,
        )
        return await self._rest(
            Payer, "POST", f"/api/payers/{payer_id}/funding-sources", json=body, idempotency_key=idempotency_key
        )

    async def set_default_funding_source(self, payer_id: str, /, *, source_id: str) -> Payer:
        """Choose the funding source mandates debit by default.

        Served by the MCP tool ``set_default_funding_source`` (no REST route);
        not replay-safe.
        """
        payer_id = _path_id(payer_id, field="payer_id")
        require_id(source_id, field="source_id")
        return await self._tool(Payer, "set_default_funding_source", {"payerId": payer_id, "sourceId": source_id})

    async def get(self, payer_id: str, /) -> Payer:
        """Read a payer (``GET /api/payers/{id}``)."""
        payer_id = _path_id(payer_id, field="payer_id")
        return await self._rest(Payer, "GET", f"/api/payers/{payer_id}")

    async def list(self, *, status: PayerStatus | str | None = None) -> list[Payer]:
        """List every payer (``GET /api/payers``); ``status`` is filtered client-side."""
        wanted = validated_enum(status, PayerStatus, field="status") if status is not None else None
        payers = _parse_list(Payer, await self._transport.request("GET", "/api/payers"), "payers")
        if wanted is None:
            return payers
        return [payer for payer in payers if payer.status == wanted]


# ---------------------------------------------------------------------- users


class UsersAPI(_Namespace):
    """People with several ways to be paid. Every method is served by the MCP endpoint; not replay-safe."""

    async def create(
        self,
        *,
        first_name: str,
        last_name: str,
        email: str,
        phone: str,
        payment_methods: Sequence[PaymentMethodInput | Mapping[str, Any]],
    ) -> User:
        """Register a user (MCP ``register_user``); the first method becomes the default."""
        arguments = {
            "firstName": require_str(first_name, field="first_name"),
            "lastName": require_str(last_name, field="last_name"),
            "email": require_str(email, field="email"),
            "phone": require_str(phone, field="phone"),
            "paymentMethods": _inputs(payment_methods, PaymentMethodInput, field="payment_methods"),
        }
        return await self._tool(User, "register_user", arguments)

    async def add_payment_method(
        self,
        user_id: str,
        /,
        *,
        type: FundingSourceType | str,
        destination: str,
        holder_name: str | None = None,
        label: str | None = None,
        make_default: bool = False,
    ) -> User:
        """Add a payment method (MCP ``add_payment_method``)."""
        require_id(user_id, field="user_id")
        arguments = _body(
            userId=user_id,
            type=enum_value(type),
            destination=require_str(destination, field="destination"),
            holderName=holder_name,
            label=label,
            makeDefault=True if make_default else None,
        )
        return await self._tool(User, "add_payment_method", arguments)

    async def set_default_payment_method(self, user_id: str, /, *, method_id: str) -> User:
        """Choose the default payment method (MCP ``set_default_payment_method``)."""
        require_id(user_id, field="user_id")
        require_id(method_id, field="method_id")
        return await self._tool(User, "set_default_payment_method", {"userId": user_id, "methodId": method_id})

    async def get(self, user_id: str, /) -> User:
        """Read a user (MCP ``get_user``)."""
        require_id(user_id, field="user_id")
        return await self._tool(User, "get_user", {"userId": user_id})

    async def list(self) -> list[User]:
        """List every user, newest first (MCP ``list_users``)."""
        try:
            payload = await self._client.mcp.call_tool("list_users", {})
        except (ToolError, MCPProtocolError) as exc:
            raise _translate_mcp_error(exc) from None
        return [User.from_wire(item) for item in payload.get("users", []) if isinstance(item, dict)]

    async def create_beneficiary(self, user_id: str, /, *, method_id: str | None = None) -> Beneficiary:
        """Turn a user into a payout beneficiary (MCP ``create_beneficiary_for_user``).

        Reuses any existing beneficiary with that IBAN, possibly created under
        another name; refuses ``x402_wallet`` methods on SEPA deployments.
        """
        require_id(user_id, field="user_id")
        arguments = _body(userId=user_id, methodId=method_id)
        return await self._tool(Beneficiary, "create_beneficiary_for_user", arguments)


# ---------------------------------------------------------------------- beneficiaries


class BeneficiariesAPI(_Namespace):
    """Who can be paid."""

    async def create(
        self,
        *,
        full_name: str,
        iban: str,
        reference: str,
        email: str | None = None,
        wallet_address: str | None = None,
        idempotency_key: str | None = None,
    ) -> Beneficiary:
        """Register a beneficiary (``POST /api/beneficiaries``); creates a new record every time.

        ``wallet_address`` is accepted but not returned by the server today.
        """
        body = _body(
            fullName=require_str(full_name, field="full_name"),
            iban=require_str(iban, field="iban"),
            reference=require_str(reference, field="reference"),
            email=email,
            walletAddress=wallet_address,
        )
        return await self._rest(Beneficiary, "POST", "/api/beneficiaries", json=body, idempotency_key=idempotency_key)

    async def get(self, beneficiary_id: str, /) -> Beneficiary:
        """Read a beneficiary (MCP ``get_beneficiary``; there is no REST route)."""
        require_id(beneficiary_id, field="beneficiary_id")
        return await self._tool(Beneficiary, "get_beneficiary", {"beneficiaryId": beneficiary_id})

    async def validate_iban(
        self, *, iban: str, country_code: str | None = None, idempotency_key: str | None = None
    ) -> IbanValidation:
        """Check an IBAN's format and checksum (``POST /api/validate-iban``; 200 even when invalid)."""
        body = _body(iban=require_str(iban, field="iban"), countryCode=country_code)
        return await self._rest(
            IbanValidation, "POST", "/api/validate-iban", json=body, idempotency_key=idempotency_key
        )


# ---------------------------------------------------------------------- mandates


class MandatesAPI(_Namespace):
    """Signed standing authorizations."""

    async def create(
        self,
        *,
        beneficiary_id: str,
        mandate_reference: str,
        signed_by: str,
        max_amount: Amount,
        currency: str = "EUR",
        max_total_amount: Amount | None = None,
        payer_id: str | None = None,
        funding_source_id: str | None = None,
        debtor_name: str | None = None,
        debtor_iban: str | None = None,
        scheme: MandateScheme | str | None = None,
        mandate_type: MandateType | str | None = None,
        rail: Rail | str | None = None,
        valid_from: DateLike | None = None,
        valid_until: DateLike | None = None,
        signed_at: DateLike | None = None,
        idempotency_key: str | None = None,
    ) -> Mandate:
        """Sign a mandate (``POST /api/mandates``).

        Give ``payer_id`` (the debtor comes from the verified payer) or both
        ``debtor_name`` and ``debtor_iban``; when both are given the server uses
        the payer. ``funding_source_id`` selects the debited source but is not
        echoed (read ``debtor_iban``). Validity is capped at 1095 days.

        ``rail`` is sent for forward compatibility, but the service currently
        records every mandate on the SEPA rail and ignores it; read
        ``mandate.rail`` on the result instead of assuming the requested rail.
        """
        _check_debtor(payer_id, debtor_name, debtor_iban)
        window = _validity_window(valid_from, valid_until)
        body = _body(
            beneficiaryId=require_id(beneficiary_id, field="beneficiary_id"),
            mandateReference=validate_mandate_reference(mandate_reference),
            payerId=payer_id,
            fundingSourceId=funding_source_id,
            debtorName=debtor_name,
            debtorIban=debtor_iban,
            signedBy=require_str(signed_by, field="signed_by"),
            currency=upper_currency(currency, default="EUR"),
            maxAmount=amount_to_json(to_amount(max_amount, field="max_amount")),
            maxTotalAmount=(
                amount_to_json(to_amount(max_total_amount, field="max_total_amount"))
                if max_total_amount is not None
                else None
            ),
            scheme=enum_value(scheme),
            mandateType=enum_value(mandate_type),
            rail=enum_value(rail),
            validFrom=window[0],
            validUntil=window[1],
            signedAt=to_iso_datetime(signed_at, field="signed_at") if signed_at is not None else None,
        )
        return await self._rest(Mandate, "POST", "/api/mandates", json=body, idempotency_key=idempotency_key)

    async def validate(
        self,
        mandate_id: str,
        /,
        *,
        amount: Amount | None = None,
        currency: str | None = None,
        beneficiary_id: str | None = None,
        payout_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> MandateValidation:
        """Run the ten checks against a possible payment without deciding anything."""
        mandate_id = _path_id(mandate_id, field="mandate_id")
        body = _body(
            amount=amount_to_json(to_amount(amount)) if amount is not None else None,
            currency=upper_currency(currency),
            beneficiaryId=beneficiary_id,
            payoutId=payout_id,
        )
        return await self._rest(
            MandateValidation, "POST", f"/api/mandates/{mandate_id}/validate", json=body, idempotency_key=idempotency_key
        )

    async def revoke(self, mandate_id: str, /, *, reason: str | None = None, idempotency_key: str | None = None) -> Mandate:
        """Revoke a mandate; every later decision under it is refused."""
        mandate_id = _path_id(mandate_id, field="mandate_id")
        return await self._rest(
            Mandate, "POST", f"/api/mandates/{mandate_id}/revoke", json=_body(reason=reason), idempotency_key=idempotency_key
        )

    async def get(self, mandate_id: str, /) -> Mandate:
        """Read a mandate (``GET /api/mandates/{id}``); may record ``expired`` as a side effect."""
        mandate_id = _path_id(mandate_id, field="mandate_id")
        return await self._rest(Mandate, "GET", f"/api/mandates/{mandate_id}")

    async def list(
        self,
        *,
        status: MandateStatus | str | None = None,
        payer_id: str | None = None,
        beneficiary_id: str | None = None,
    ) -> list[Mandate]:
        """List every mandate (``GET /api/mandates``); all filters are applied client-side."""
        wanted = validated_enum(status, MandateStatus, field="status") if status is not None else None
        mandates = _parse_list(Mandate, await self._transport.request("GET", "/api/mandates"), "mandates")
        return [
            mandate
            for mandate in mandates
            if (wanted is None or mandate.status == wanted)
            and (payer_id is None or mandate.payer_id == payer_id)
            and (beneficiary_id is None or mandate.beneficiary_id == beneficiary_id)
        ]


def _check_debtor(payer_id: str | None, debtor_name: str | None, debtor_iban: str | None) -> None:
    if payer_id is None and not (debtor_name and debtor_iban):
        raise InvalidInputError("pass payer_id, or both debtor_name and debtor_iban")
    if (debtor_name is None) != (debtor_iban is None):
        raise InvalidInputError("debtor_name and debtor_iban go together")


def _validity_window(valid_from: DateLike | None, valid_until: DateLike | None) -> tuple[str | None, str | None]:
    start = to_iso_datetime(valid_from, field="valid_from") if valid_from is not None else None
    end = to_iso_datetime(valid_until, field="valid_until") if valid_until is not None else None
    if valid_from is not None and valid_until is not None:
        span = _as_datetime(valid_until) - _as_datetime(valid_from)
        if span > timedelta(days=MAX_MANDATE_DURATION_DAYS):
            raise InvalidInputError(f"valid_from..valid_until spans more than {MAX_MANDATE_DURATION_DAYS} days")
        if span.total_seconds() < 0:
            raise InvalidInputError("valid_until must not be before valid_from")
    return start, end


def _as_datetime(value: DateLike) -> datetime:
    """Naive UTC datetime for span arithmetic (inputs were validated already)."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    else:
        text = value.strip()
        text = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.combine(date.fromisoformat(text), datetime.min.time())
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


# ---------------------------------------------------------------------- authorizations


class AuthorizationsAPI(_Namespace):
    """Signed decisions: may this payment proceed under this mandate?"""

    async def create(
        self,
        *,
        mandate_id: str,
        amount: Amount,
        currency: str | None = None,
        beneficiary_id: str | None = None,
        payee: str | None = None,
        payee_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> AuthorizationReceipt:
        """Ask for a signed receipt (``POST /api/authorize``); moves and reserves nothing.

        ``beneficiary_id`` or ``payee`` (an IBAN) is required; ``beneficiary_id``
        is the reliable path.
        """
        if beneficiary_id is None and payee is None:
            raise InvalidInputError("pass beneficiary_id or payee")
        body = _body(
            mandateId=require_id(mandate_id, field="mandate_id"),
            amount=amount_to_json(to_amount(amount)),
            currency=upper_currency(currency),
            beneficiaryId=beneficiary_id,
            payee=payee,
            payeeName=payee_name,
        )
        return await self._rest(AuthorizationReceipt, "POST", "/api/authorize", json=body, idempotency_key=idempotency_key)

    async def verify(
        self, receipt: AuthorizationReceipt | Mapping[str, Any], /, *, idempotency_key: str | None = None
    ) -> ReceiptVerification:
        """Verify a receipt exactly as received (``POST /api/authorize/verify``).

        A model sends its ``.raw`` verbatim and a mapping is sent untouched; the
        signature is byte-sensitive, so the receipt is never re-serialized
        through the model.
        """
        if isinstance(receipt, AuthorizationReceipt):
            if receipt.raw is None:
                raise InvalidInputError("pass the receipt exactly as received (dict or .raw)")
            payload: Mapping[str, Any] = receipt.raw
        elif isinstance(receipt, Mapping):
            payload = receipt
        else:
            raise InvalidInputError("receipt must be an AuthorizationReceipt or a mapping")
        return await self._rest(
            ReceiptVerification, "POST", "/api/authorize/verify", json={"receipt": payload}, idempotency_key=idempotency_key
        )


# ---------------------------------------------------------------------- payouts


class PayoutsAPI(_Namespace):
    """The payout lifecycle: draft → pending_kyc → approved → processing → paid."""

    async def create(
        self,
        *,
        beneficiary_id: str,
        amount: Amount,
        currency: str = "EUR",
        reason: str | None = None,
        mandate_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Payout:
        """Create a draft (``POST /api/payouts``); amounts have at most two decimals."""
        body = _body(
            beneficiaryId=require_id(beneficiary_id, field="beneficiary_id"),
            mandateId=mandate_id,
            amount=amount_to_json(to_amount(amount)),
            currency=upper_currency(currency, default="EUR"),
            reason=reason,
        )
        return await self._rest(Payout, "POST", "/api/payouts", json=body, idempotency_key=idempotency_key)

    async def submit(self, payout_id: str, /, *, idempotency_key: str | None = None) -> PayoutSubmission:
        """Send the draft to KYC review (``POST /api/payouts/{id}/submit``); then poll with :meth:`wait_for_kyc`."""
        payout_id = _path_id(payout_id, field="payout_id")
        return await self._rest(
            PayoutSubmission, "POST", f"/api/payouts/{payout_id}/submit", json={}, idempotency_key=idempotency_key
        )

    async def record_event(
        self, payout_id: str, /, *, event: ProviderEvent | str, idempotency_key: str | None = None
    ) -> PayoutTransition:
        """Record a provider decision yourself (``POST /api/payouts/{id}/events``)."""
        payout_id = _path_id(payout_id, field="payout_id")
        body = {"event": validated_enum(event, ProviderEvent, field="event")}
        return await self._rest(
            PayoutTransition, "POST", f"/api/payouts/{payout_id}/events", json=body, idempotency_key=idempotency_key
        )

    async def confirm(self, payout_id: str, /, *, idempotency_key: str | None = None) -> PayoutTransition:
        """Human confirmation (``POST /api/payouts/{id}/confirm``): ``approved`` → ``processing``; settles nothing."""
        payout_id = _path_id(payout_id, field="payout_id")
        return await self._rest(
            PayoutTransition, "POST", f"/api/payouts/{payout_id}/confirm", json={}, idempotency_key=idempotency_key
        )

    async def execute(self, payout_id: str, /, *, idempotency_key: str | None = None) -> PayoutExecution:
        """Send the payout through the rail (``POST /api/payouts/{id}/execute``).

        Works from ``approved`` or ``processing``. A 400 raises
        :class:`~whire.exceptions.PayoutExecutionRefused`: the payout may now be
        ``failed`` or unchanged, so call :meth:`get` and let a human decide. To
        retry an execute that failed on the network, reuse ``e.idempotency_key``;
        never resend without a key.
        """
        payout_id = _path_id(payout_id, field="payout_id")
        try:
            return await self._rest(
                PayoutExecution,
                "POST",
                f"/api/payouts/{payout_id}/execute",
                json={},
                idempotency_key=idempotency_key,
                timeout_override=self._client.execute_timeout,
            )
        except BadRequestError as exc:
            raise PayoutExecutionRefused(
                exc.message, status_code=exc.status_code, request_id=exc.request_id, idempotency_key=exc.idempotency_key
            ) from None

    async def get(self, payout_id: str, /) -> Payout:
        """Read a payout with its history (``GET /api/payouts/{id}``)."""
        payout_id = _path_id(payout_id, field="payout_id")
        return await self._rest(Payout, "GET", f"/api/payouts/{payout_id}")

    async def list(
        self,
        *,
        status: PayoutStatus | str | None = None,
        mandate_id: str | None = None,
        beneficiary_id: str | None = None,
    ) -> list[Payout]:
        """List payouts (``GET /api/payouts[?status=]``).

        ``status`` is validated locally and filtered by the server;
        ``mandate_id`` / ``beneficiary_id`` are filtered client-side.
        """
        query = {"status": validated_enum(status, PayoutStatus, field="status")} if status is not None else None
        result = await self._transport.request("GET", "/api/payouts", query=query)
        payouts = _parse_list(Payout, result, "payouts")
        return [
            payout
            for payout in payouts
            if (mandate_id is None or payout.mandate_id == mandate_id)
            and (beneficiary_id is None or payout.beneficiary_id == beneficiary_id)
        ]

    async def wait(
        self,
        payout_id: str,
        /,
        *,
        status: PayoutStatus | str | Iterable[PayoutStatus | str] | None = None,
        until: Callable[[Payout], bool] | None = None,
        timeout: float = 60.0,
        interval: float = 1.0,
        max_interval: float = 5.0,
    ) -> Payout:
        """Poll ``get()`` until the payout reaches ``status`` or ``until(payout)`` is true.

        Exactly one of ``status`` / ``until`` must be given. Polling starts at
        ``interval`` (≥ 0.25 s) and grows ×1.5 up to ``max_interval`` with ±20 %
        jitter. Retryable errors are tolerated until the deadline; others abort.

        Raises:
            WhireTimeoutError: with ``.last`` (the last payout read) on expiry.
        """
        predicate = _wait_predicate(status, until)
        payout_id = _path_id(payout_id, field="payout_id")
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        sleep = max(0.25, float(interval))
        last: Payout | None = None
        while True:
            try:
                last = await self.get(payout_id)
                if predicate(last):
                    return last
            except WhireError as exc:
                if not exc.is_retryable:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, sleep * random.uniform(0.8, 1.2)))
            sleep = min(sleep * 1.5, max_interval)
        elapsed = time.monotonic() - started
        raise WhireTimeoutError(
            f"payout {payout_id} did not reach the wanted state within {timeout:g}s"
            + (f" (last status: {last.status})" if last is not None else ""),
            payout_id=payout_id,
            last=last,
            elapsed=elapsed,
        )

    async def wait_for_kyc(self, payout_id: str, /, **kwargs: Any) -> Payout:
        """Wait until the payout leaves ``pending_kyc`` (``approved`` or ``kyc_rejected``)."""
        return await self.wait(payout_id, until=lambda p: p.status != PayoutStatus.PENDING_KYC, **kwargs)

    async def wait_for_settlement(self, payout_id: str, /, **kwargs: Any) -> Payout:
        """Wait until the payout leaves ``processing``.

        Call this after ``execute()`` returned ``processing`` (or ``unresolved``).
        After ``confirm()`` alone nothing settles, and a frozen-source refusal
        after confirm stays ``processing`` until the timeout.
        """
        return await self.wait(payout_id, until=lambda p: p.status != PayoutStatus.PROCESSING, **kwargs)


def _wait_predicate(
    status: PayoutStatus | str | Iterable[PayoutStatus | str] | None,
    until: Callable[[Payout], bool] | None,
) -> Callable[[Payout], bool]:
    if (status is None) == (until is None):
        raise ValueError("pass exactly one of status= or until=")
    if until is not None:
        return until
    values = [status] if isinstance(status, str) else list(status or [])
    wanted = {validated_enum(value, PayoutStatus, field="status") for value in values}
    if not wanted:
        raise ValueError("status must name at least one payout status")
    return lambda payout: str(payout.status) in wanted


# ---------------------------------------------------------------------- simulation / service


class SimulationAPI(_Namespace):
    """The simulated rail (only meaningful when ``capabilities().simulated``)."""

    async def get(self) -> Simulation:
        """Scenarios, delay and simulated accounts (``GET /api/simulation``)."""
        return await self._rest(Simulation, "GET", "/api/simulation")

    async def reset(self, *, force: bool = False) -> ResetResult:
        """Empty the store and the simulated ledger (``POST /api/reset``).

        Refused unless ``capabilities()`` reports ``environment == "SANDBOX"``
        and ``simulated is True``, and always refused against ``api.whire.ai``;
        ``force=True`` skips both guards. The store is shared: every caller's
        records disappear.
        """
        if not force:
            self._guard_reset(await self._client.capabilities())
        return await self._rest(ResetResult, "POST", "/api/reset", json={})

    def _guard_reset(self, capabilities: Capabilities) -> None:
        if is_production_host(urlsplit(self._client.base_url).hostname):
            raise InvalidInputError("reset is only allowed against a simulated sandbox deployment")
        if capabilities.environment != "SANDBOX" or capabilities.simulated is not True:
            raise InvalidInputError("reset is only allowed against a simulated sandbox deployment")


# ---------------------------------------------------------------------- x402


class X402API(_Namespace):
    """x402 over SEPA: the facilitator side (REST) and the payer side (MCP)."""

    async def supported(self) -> X402Supported:
        """What this deployment can verify and settle (``GET /x402/supported``, plain body)."""
        result = await self._transport.request("GET", "/x402/supported", envelope=False)
        return X402Supported.from_wire(result.data, replayed=result.replayed)

    async def verify(
        self,
        *,
        payment_payload: X402PaymentPayload | Mapping[str, Any],
        payment_requirements: X402PaymentRequirements | Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> X402VerifyResult:
        """Verify a decoded ``PAYMENT-SIGNATURE`` (``POST /x402/verify``); never raises on ``is_valid=False``."""
        body = _x402_body(payment_payload, payment_requirements)
        result = await self._transport.request(
            "POST", "/x402/verify", json=body, envelope=False, idempotency_key=idempotency_key
        )
        return X402VerifyResult.from_wire(result.data, replayed=result.replayed)

    async def settle(
        self,
        *,
        payment_payload: X402PaymentPayload | Mapping[str, Any],
        payment_requirements: X402PaymentRequirements | Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> X402SettleResult:
        """Verify again and execute the payout (``POST /x402/settle``).

        Idempotent per signed payload, so it is retried like a read. Never
        raises on ``success=False``; when ``.pending`` is true settle again
        later with the same payload and never re-pay.
        """
        body = _x402_body(payment_payload, payment_requirements)
        result = await self._transport.request(
            "POST",
            "/x402/settle",
            json=body,
            envelope=False,
            idempotency_key=idempotency_key,
            timeout_override=self._client.execute_timeout,
            idempotent=True,
        )
        return X402SettleResult.from_wire(result.data, replayed=result.replayed)

    async def quote(self, *, url: str) -> X402Quote:
        """Read a 402 price without committing (MCP ``quote_x402_resource``)."""
        return await self._tool(X402Quote, "quote_x402_resource", {"url": require_str(url, field="url")})

    async def pay(self, *, url: str, mandate_id: str, reason: str | None = None) -> X402Payment:
        """Pay a 402-gated URL under a mandate (MCP ``pay_x402_resource``).

        Destructive and not idempotent: never repeated by the SDK. A business
        refusal raises :class:`~whire.exceptions.PayoutExecutionRefused`.
        """
        arguments = _body(
            url=require_str(url, field="url"), mandateId=require_id(mandate_id, field="mandate_id"), reason=reason
        )
        try:
            return await self._tool(X402Payment, "pay_x402_resource", arguments)
        except BadRequestError as exc:
            if exc.error_code == "invalid_arguments":
                raise
            raise PayoutExecutionRefused(
                exc.message,
                status_code=None,
                request_id=exc.request_id,
                idempotency_key=exc.idempotency_key,
                suggestion=(
                    "pay_x402_resource did not complete; unless the message names a payout, nothing was "
                    "created or sent (e.g. 'fetch failed' means the seller URL was unreachable). Check the "
                    "URL and mandate and ask a human before paying again."
                ),
            ) from None


def _x402_body(
    payment_payload: X402PaymentPayload | Mapping[str, Any],
    payment_requirements: X402PaymentRequirements | Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "x402Version": 2,
        "paymentPayload": _model_payload(payment_payload, field="payment_payload"),
        "paymentRequirements": _model_payload(payment_requirements, field="payment_requirements"),
    }
