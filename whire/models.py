"""Pydantic models for the agent-payouts API.

Attributes are ``snake_case``; the wire uses ``camelCase`` (handled by the
alias generator). Money fields are :class:`~decimal.Decimal`; timestamps are
timezone-aware UTC. Status-like fields keep unknown values as plain strings so
a newer server never breaks parsing. Every model returned by a request carries
``.raw`` (the exact decoded wire object) and ``.replayed``.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Annotated, Any, TypeVar

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
)
from pydantic.alias_generators import to_camel

from whire.exceptions import ResponseFormatError

__all__ = [
    "Amount",
    "WhireModel",
    "AccountType",
    "PayerStatus",
    "FundingSourceType",
    "MandateScheme",
    "MandateType",
    "Rail",
    "MandateStatus",
    "Decision",
    "PayoutStatus",
    "ProviderEvent",
    "MandateCheck",
    "MANDATE_REFERENCE_MAX_LENGTH",
    "MAX_MANDATE_DURATION_DAYS",
    "TERMINAL_PAYOUT_STATUSES",
    "HistoryEntry",
    "FundingSource",
    "FundingSourceInput",
    "Payer",
    "PaymentMethod",
    "PaymentMethodInput",
    "User",
    "Beneficiary",
    "IbanValidation",
    "Mandate",
    "CheckResult",
    "MandateUsage",
    "MandateValidation",
    "AuthorizationReceipt",
    "ReceiptVerification",
    "Payout",
    "PayoutSubmission",
    "PayoutTransition",
    "PayoutExecution",
    "SimulationScenario",
    "SimulationAccount",
    "SimulationTriggers",
    "Simulation",
    "ResetResult",
    "PaymentProviderInfo",
    "Health",
    "Capabilities",
    "X402Kind",
    "X402Supported",
    "X402PaymentRequirements",
    "X402Authorization",
    "X402PaymentPayload",
    "X402VerifyResult",
    "X402SettleResult",
    "X402Quote",
    "X402Payment",
    "ToolDefinition",
    "ResourceInfo",
    "ResourceTemplateInfo",
    "ResourceContent",
    "ResourceContents",
    "PromptArgument",
    "PromptInfo",
    "PromptMessage",
    "ServerInfo",
    "Completion",
]

Amount = Decimal | int | float | str
"""Accepted input types for money amounts (validated to ≤ 2 decimals, > 0)."""

MANDATE_REFERENCE_MAX_LENGTH = 35
MAX_MANDATE_DURATION_DAYS = 1095

M = TypeVar("M", bound="WhireModel")


# --------------------------------------------------------------------------- enums


class AccountType(StrEnum):
    BUSINESS = "business"
    INDIVIDUAL = "individual"


class PayerStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class FundingSourceType(StrEnum):
    SEPA = "sepa"
    X402_WALLET = "x402_wallet"


class MandateScheme(StrEnum):
    SEPA_CORE = "sepa_core"
    SEPA_B2B = "sepa_b2b"
    AGENT_PAYOUT = "agent_payout"


class MandateType(StrEnum):
    ONE_OFF = "one_off"
    RECURRING = "recurring"


class Rail(StrEnum):
    SEPA = "sepa"
    X402 = "x402"


class MandateStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class Decision(StrEnum):
    APPROVED = "approved"
    REFUSED = "refused"


class PayoutStatus(StrEnum):
    DRAFT = "draft"
    PENDING_KYC = "pending_kyc"
    KYC_REJECTED = "kyc_rejected"
    APPROVED = "approved"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"
    RETURNED = "returned"


class ProviderEvent(StrEnum):
    KYC_APPROVED = "kyc_approved"
    KYC_REJECTED = "kyc_rejected"
    PAYMENT_PROCESSING = "payment_processing"
    PAYMENT_PAID = "payment_paid"
    PAYMENT_FAILED = "payment_failed"
    PAYMENT_RETURNED = "payment_returned"


class MandateCheck(StrEnum):
    """The ten mandate checks, in the order the service runs them."""

    MANDATE_REFERENCE_FORMAT = "mandate_reference_format"
    DEBTOR_SOURCE_VALID = "debtor_source_valid"
    SIGNATURE_INTACT = "signature_intact"
    MANDATE_ACTIVE = "mandate_active"
    VALIDITY_WINDOW = "validity_window"
    MANDATE_TYPE_USAGE = "mandate_type_usage"
    CURRENCY_MATCH = "currency_match"
    BENEFICIARY_MATCH = "beneficiary_match"
    PER_PAYMENT_LIMIT = "per_payment_limit"
    CUMULATIVE_LIMIT = "cumulative_limit"


TERMINAL_PAYOUT_STATUSES: frozenset[PayoutStatus] = frozenset(
    {PayoutStatus.PAID, PayoutStatus.FAILED, PayoutStatus.RETURNED, PayoutStatus.KYC_REJECTED}
)


def _lenient(enum_cls: type[Enum]) -> BeforeValidator:
    """Return the enum member when the value is known, the raw value otherwise."""

    def convert(value: Any) -> Any:
        if isinstance(value, enum_cls) or not isinstance(value, str):
            return value
        try:
            return enum_cls(value)
        except ValueError:
            return value

    return BeforeValidator(convert)


AccountTypeField = Annotated[AccountType | str, _lenient(AccountType)]
PayerStatusField = Annotated[PayerStatus | str, _lenient(PayerStatus)]
FundingSourceTypeField = Annotated[FundingSourceType | str, _lenient(FundingSourceType)]
MandateSchemeField = Annotated[MandateScheme | str, _lenient(MandateScheme)]
MandateTypeField = Annotated[MandateType | str, _lenient(MandateType)]
RailField = Annotated[Rail | str, _lenient(Rail)]
MandateStatusField = Annotated[MandateStatus | str, _lenient(MandateStatus)]
DecisionField = Annotated[Decision | str, _lenient(Decision)]
PayoutStatusField = Annotated[PayoutStatus | str, _lenient(PayoutStatus)]
MandateCheckField = Annotated[MandateCheck | str, _lenient(MandateCheck)]


# --------------------------------------------------------------------------- base


class WhireModel(BaseModel):
    """Base of every SDK model.

    ``raw`` is the verbatim wire object the model was parsed from (``None``
    for locally constructed instances); ``replayed`` is ``True`` when the
    server answered from its idempotency store.
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel, extra="allow")

    _raw: dict[str, Any] | None = PrivateAttr(default=None)
    _replayed: bool = PrivateAttr(default=False)

    @property
    def raw(self) -> dict[str, Any] | None:
        """The exact decoded wire object, or ``None`` when built locally."""
        return self._raw

    @property
    def replayed(self) -> bool:
        """Whether the server replayed a stored idempotent response."""
        return self._replayed

    def to_dict(self) -> dict[str, Any]:
        """Dump with wire (camelCase) names in JSON mode.

        ``Decimal`` becomes a string and ``datetime`` an ISO string with
        microseconds, which differs from the wire; use ``.raw`` when the exact
        wire payload matters (signatures, replaying a receipt).
        """
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_wire(cls: type[M], data: Any, *, replayed: bool = False) -> M:
        """Parse a decoded wire object, attaching ``raw`` and ``replayed``.

        Raises:
            ResponseFormatError: when the object does not match the model.
        """
        if not isinstance(data, dict):
            raise ResponseFormatError(
                f"expected a JSON object for {cls.__name__}, got {type(data).__name__}", payload=data
            )
        try:
            instance = cls.model_validate(data)
        except ValidationError as exc:
            raise ResponseFormatError(
                f"{cls.__name__} does not match the response: {exc.errors()[0].get('msg', exc)}",
                payload=data,
            ) from None
        instance._raw = data
        instance._replayed = replayed
        return instance


# --------------------------------------------------------------------------- payers


class HistoryEntry(WhireModel):
    status: str
    timestamp: datetime
    note: str | None = None


class FundingSource(WhireModel):
    source_id: str
    type: FundingSourceTypeField
    label: str | None = None
    identifier: str
    holder_name: str | None = None
    is_default: bool = False
    verified: bool = False
    created_at: datetime | None = None


class FundingSourceInput(WhireModel):
    """A funding source to register on a payer (``type`` is ``sepa`` or ``x402_wallet``)."""

    type: FundingSourceTypeField
    destination: str
    holder_name: str | None = None
    label: str | None = None
    make_default: bool = False


class Payer(WhireModel):
    payer_id: str
    account_type: AccountTypeField
    legal_name: str
    registration_number: str | None = None
    vat_number: str | None = None
    contact_first_name: str
    contact_last_name: str
    email: str
    phone: str
    funding_sources: list[FundingSource] = Field(default_factory=list)
    status: PayerStatusField
    history: list[HistoryEntry] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @property
    def default_funding_source(self) -> FundingSource | None:
        """The funding source mandates debit by default."""
        for source in self.funding_sources:
            if source.is_default:
                return source
        return self.funding_sources[0] if self.funding_sources else None


# --------------------------------------------------------------------------- users


class PaymentMethod(WhireModel):
    method_id: str
    type: FundingSourceTypeField
    label: str | None = None
    destination: str
    holder_name: str | None = None
    is_default: bool = False
    verified: bool = False
    created_at: datetime | None = None


class PaymentMethodInput(WhireModel):
    """A payment method to register on a user (``type`` is ``sepa`` or ``x402_wallet``)."""

    type: FundingSourceTypeField
    destination: str
    holder_name: str | None = None
    label: str | None = None
    make_default: bool = False


class User(WhireModel):
    user_id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    payment_methods: list[PaymentMethod] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @property
    def default_payment_method(self) -> PaymentMethod | None:
        """The payment method payouts use by default."""
        for method in self.payment_methods:
            if method.is_default:
                return method
        return self.payment_methods[0] if self.payment_methods else None


# --------------------------------------------------------------------------- beneficiaries


class Beneficiary(WhireModel):
    """A payee. ``wallet_address`` is accepted on create but not returned today."""

    beneficiary_id: str
    full_name: str
    iban: str
    reference: str
    email: str | None = None
    wallet_address: str | None = None
    created_at: datetime


class IbanValidation(WhireModel):
    original_iban: str
    normalized_iban: str
    is_valid: bool
    country_code: str | None = None
    format_valid: bool
    checksum_valid: bool
    explanation: str


# --------------------------------------------------------------------------- mandates


class Mandate(WhireModel):
    mandate_id: str
    mandate_reference: str
    beneficiary_id: str
    payer_id: str | None = None
    debtor_name: str
    debtor_iban: str
    scheme: MandateSchemeField
    mandate_type: MandateTypeField
    rail: RailField
    currency: str
    max_amount: Decimal
    max_total_amount: Decimal | None = None
    valid_from: datetime
    valid_until: datetime
    signed_by: str
    signed_at: datetime
    signature: str
    status: MandateStatusField
    created_at: datetime
    updated_at: datetime
    history: list[HistoryEntry] = Field(default_factory=list)


class CheckResult(WhireModel):
    name: MandateCheckField
    passed: bool
    detail: str


class MandateUsage(WhireModel):
    payout_count: int
    total_amount: Decimal


class MandateValidation(WhireModel):
    mandate_id: str
    mandate_reference: str
    status: MandateStatusField
    scheme: MandateSchemeField
    mandate_type: MandateTypeField
    is_valid: bool
    checks: list[CheckResult] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)
    usage: MandateUsage
    remaining_amount: Decimal | None = None
    evaluated_context: dict[str, Any] | None = None
    evaluated_at: datetime
    explanation: str


# --------------------------------------------------------------------------- authorization


class AuthorizationReceipt(WhireModel):
    """A signed decision. Pass ``.raw`` (never ``to_dict()``) to ``verify``."""

    receipt_id: str
    decision: DecisionField
    mandate_id: str
    mandate_reference: str
    payer: str
    payee: str
    payee_name: str | None = None
    amount: Decimal
    currency: str
    checks: list[CheckResult] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)
    explanation: str
    remaining_amount: Decimal | None = None
    issued_at: datetime
    expires_at: datetime
    funds_reserved: bool = False
    signature: str

    @property
    def approved(self) -> bool:
        """Whether the decision records an approval (a permission, not a reservation)."""
        return self.decision == Decision.APPROVED


class ReceiptVerification(WhireModel):
    signature_valid: bool
    expired: bool
    decision: DecisionField
    usable: bool
    explanation: str


# --------------------------------------------------------------------------- payouts


class Payout(WhireModel):
    payout_id: str
    beneficiary_id: str
    mandate_id: str | None = None
    amount: Decimal
    currency: str
    reason: str | None = None
    provider: str
    provider_reference: str | None = None
    executed_at: datetime | None = None
    status: PayoutStatusField
    created_at: datetime
    updated_at: datetime
    history: list[HistoryEntry] = Field(default_factory=list)

    @property
    def last_note(self) -> str | None:
        """The note of the most recent history entry, if any."""
        for entry in reversed(self.history):
            if entry.note:
                return entry.note
        return None

    @property
    def is_terminal(self) -> bool:
        """Whether the payout can no longer change (paid, failed, returned, kyc_rejected)."""
        return self.status in TERMINAL_PAYOUT_STATUSES


class PayoutSubmission(WhireModel):
    payout_id: str
    status: PayoutStatusField
    provider: str
    next_action: str | None = None
    message: str | None = None


class PayoutTransition(WhireModel):
    payout_id: str
    status: PayoutStatusField
    provider: str
    message: str | None = None


class PayoutExecution(WhireModel):
    """The rail's answer to ``execute``; ``unresolved`` means: do not resend."""

    payout_id: str
    status: PayoutStatusField
    provider: str
    environment: str | None = None
    provider_reference: str | None = None
    unresolved: bool = False
    debited_account_iban: str | None = None
    counterparty_iban: str | None = None
    counterparty_name: str | None = None
    amount: Decimal
    currency: str
    balance_after: Decimal | None = None
    mandate_reference: str | None = None
    message: str | None = None


# --------------------------------------------------------------------------- simulation / service


class SimulationScenario(WhireModel):
    trigger: str
    outcome: str
    payout_status: str
    then: str | None = None


class SimulationAccount(WhireModel):
    iban: str
    balance: Decimal
    currency: str
    status: str


class SimulationTriggers(WhireModel):
    frozen_source_iban: str
    kyc_rejected_iban: str


class Simulation(WhireModel):
    enabled: bool
    note: str | None = None
    rail: str | None = None
    environment: str | None = None
    currency: str | None = None
    delay_ms: int | None = None
    opening_balance: Decimal | None = None
    triggers: SimulationTriggers | None = None
    scenarios: list[SimulationScenario] = Field(default_factory=list)
    accounts: list[SimulationAccount] = Field(default_factory=list)


class ResetResult(WhireModel):
    payouts: list[Any] = Field(default_factory=list)


class PaymentProviderInfo(WhireModel):
    configured: bool
    rail: str | None = None
    environment: str | None = None
    can_move_money: bool = False
    simulated: bool = False


class Health(WhireModel):
    status: str
    transport: str | None = None
    payouts: int = 0
    mandates: int = 0
    payment_provider: PaymentProviderInfo | None = None


class Capabilities(WhireModel):
    authorization: bool
    settlement: bool
    simulated: bool
    environment: str
    note: str | None = None


# --------------------------------------------------------------------------- x402


class X402Kind(WhireModel):
    x402_version: int
    scheme: str
    network: str
    extra: dict[str, Any] = Field(default_factory=dict)


class X402Supported(WhireModel):
    kinds: list[X402Kind] = Field(default_factory=list)
    extensions: list[Any] = Field(default_factory=list)
    signers: dict[str, Any] = Field(default_factory=dict)

    @property
    def can_settle(self) -> bool:
        """Whether the deployment advertises at least one payment kind."""
        return bool(self.kinds)


class X402PaymentRequirements(WhireModel):
    scheme: str
    network: str
    amount: str
    asset: str
    pay_to: str
    max_timeout_seconds: int
    extra: dict[str, Any] | None = None


class X402Authorization(WhireModel):
    """The signed authorization inside a payment payload (all strings, as signed)."""

    from_: str = Field(alias="from")
    to: str
    value: str
    asset: str
    valid_before: str
    nonce: str


class X402PaymentPayload(WhireModel):
    """A decoded ``PAYMENT-SIGNATURE`` payload. Signed strings stay strings."""

    x402_version: int
    resource: dict[str, Any] = Field(default_factory=dict)
    accepted: X402PaymentRequirements
    payload: dict[str, Any] = Field(default_factory=dict)


class X402VerifyResult(WhireModel):
    is_valid: bool
    invalid_reason: str | None = None
    payer: str | None = None


class X402SettleResult(WhireModel):
    success: bool
    transaction: str | None = None
    network: str | None = None
    payer: str | None = None
    settlement: str | None = None
    simulated: bool | None = None
    error_reason: str | None = None

    @property
    def pending(self) -> bool:
        """Settlement not yet decided: settle again later with the same payload; never re-pay."""
        return not self.success and (self.error_reason or "").startswith("Settlement pending:")


class X402Quote(WhireModel):
    url: str
    status: int
    free: bool
    requirement: X402PaymentRequirements | None = None
    resource_description: str | None = None
    other_offers: list[dict[str, Any]] = Field(default_factory=list)


class X402Payment(WhireModel):
    url: str
    paid: bool
    status: int
    payout_id: str | None = None
    settlement: dict[str, Any] | None = None
    body: str | None = None
    content_type: str | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- MCP metadata


class ToolDefinition(WhireModel):
    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None

    @property
    def destructive(self) -> bool:
        """Whether the server marks the tool ``destructiveHint: true``."""
        return bool((self.annotations or {}).get("destructiveHint"))


class ResourceInfo(WhireModel):
    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None


class ResourceTemplateInfo(WhireModel):
    uri_template: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None


class ResourceContent(WhireModel):
    uri: str
    text: str | None = None
    blob: str | None = None
    media_type: str | None = Field(default=None, validation_alias=AliasChoices("mediaType", "mimeType"))


class ResourceContents(WhireModel):
    contents: list[ResourceContent] = Field(default_factory=list)

    @property
    def json(self) -> Any:  # type: ignore[override]
        """The first text content parsed as JSON, or ``None``."""
        for content in self.contents:
            if content.text is None:
                continue
            try:
                return json.loads(content.text)
            except ValueError:
                continue
        return None

    @property
    def text(self) -> str | None:
        """The first text content, or ``None``."""
        for content in self.contents:
            if content.text is not None:
                return content.text
        return None


class PromptArgument(WhireModel):
    name: str
    description: str | None = None
    required: bool = False


class PromptInfo(WhireModel):
    name: str
    title: str | None = None
    description: str | None = None
    arguments: list[PromptArgument] = Field(default_factory=list)


class PromptMessage(WhireModel):
    role: str
    content: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str | None:
        """The message text when the content is ``{"type": "text", ...}``."""
        value = self.content.get("text")
        return value if isinstance(value, str) else None


class ServerInfo(WhireModel):
    name: str
    version: str
    protocol_version: str
    capabilities: dict[str, Any] = Field(default_factory=dict)
    instructions: str | None = None


class Completion(WhireModel):
    values: list[str] = Field(default_factory=list)
    total: int | None = None
    has_more: bool = False

