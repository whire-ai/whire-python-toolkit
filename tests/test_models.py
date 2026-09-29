"""Models: lenient enums, Decimal money, aware datetimes, raw/replayed/to_dict, properties (SPEC §5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

import pytest

from tests.fixtures import payloads as P
from whire import MANDATE_REFERENCE_MAX_LENGTH, MAX_MANDATE_DURATION_DAYS, TERMINAL_PAYOUT_STATUSES, Amount, ResponseFormatError
from whire import models as M

UTC = timezone.utc

FIXTURES: list[tuple[type[M.WhireModel], dict[str, Any]]] = [
    (M.Payer, P.PAYER_PENDING), (M.Payer, P.PAYER_ACTIVE), (M.FundingSource, P.FUNDING_SOURCE),
    (M.User, P.USER), (M.PaymentMethod, P.PAYMENT_METHOD), (M.Beneficiary, P.BENEFICIARY), (M.Beneficiary, P.BENEFICIARY_FOR_USER),
    (M.IbanValidation, P.IBAN_VALIDATION_INVALID), (M.Mandate, P.MANDATE), (M.Mandate, P.MANDATE_NO_PAYER), (M.Mandate, P.MANDATE_REVOKED),
    (M.MandateValidation, P.MANDATE_VALIDATION), (M.MandateValidation, P.MANDATE_VALIDATION_REFUSED),
    (M.AuthorizationReceipt, P.RECEIPT), (M.AuthorizationReceipt, P.RECEIPT_REFUSED), (M.ReceiptVerification, P.RECEIPT_VERIFICATION_TAMPERED),
    (M.Payout, P.PAYOUT_DRAFT), (M.Payout, P.PAYOUT_NO_MANDATE), (M.Payout, P.PAYOUT_PAID), (M.Payout, P.PAYOUT_FAILED),
    (M.PayoutSubmission, P.SUBMISSION), (M.PayoutTransition, P.TRANSITION_CONFIRMED),
    (M.PayoutExecution, P.EXECUTION_PAID), (M.PayoutExecution, P.EXECUTION_UNRESOLVED),
    (M.Simulation, P.SIMULATION), (M.Simulation, P.SIMULATION_DISABLED), (M.ResetResult, P.RESET), (M.Health, P.HEALTH),
    (M.Capabilities, P.CAPABILITIES), (M.Capabilities, P.CAPABILITIES_PRODUCTION),
    (M.X402Supported, P.X402_SUPPORTED), (M.X402Supported, P.X402_SUPPORTED_EMPTY),
    (M.X402PaymentRequirements, P.X402_REQUIREMENTS), (M.X402PaymentPayload, P.X402_PAYMENT_PAYLOAD),
    (M.X402VerifyResult, P.X402_VERIFY_VALID), (M.X402VerifyResult, P.X402_VERIFY_MALFORMED),
    (M.X402SettleResult, P.X402_SETTLE_SUCCESS), (M.X402SettleResult, P.X402_SETTLE_PENDING), (M.X402SettleResult, P.X402_SETTLE_MALFORMED),
    (M.X402Quote, P.X402_QUOTE_PAID), (M.X402Quote, P.X402_QUOTE_FREE), (M.X402Payment, P.X402_PAYMENT),
    (M.ResourceContents, P.MCP_RESOURCE_READ_RESULT),
]


def test_every_fixture_parses_and_keeps_raw() -> None:
    for model, data in FIXTURES:
        instance = model.from_wire(data, replayed=True)
        assert instance.raw is data and instance.replayed is True, model
        dumped = instance.to_dict()
        assert all(key in dumped for key in data), (model, set(data) - set(dumped))  # every wire key survives a by-alias dump
    items = [dict(P.PAYOUT_DRAFT), dict(P.PAYOUT_DRAFT)]
    parsed = [M.Payout.from_wire(item) for item in items]
    assert parsed[0].raw is items[0] and parsed[1].raw is items[1]  # list items get their own raw


def test_raw_replayed_and_to_dict() -> None:
    entry = M.HistoryEntry(status="draft", timestamp=datetime(2026, 9, 23, tzinfo=UTC))
    assert entry.raw is None and entry.replayed is False  # constructed locally
    assert entry.to_dict() == {"status": "draft", "timestamp": "2026-09-23T00:00:00Z", "note": None}
    dumped = M.Mandate.from_wire(P.MANDATE).to_dict()
    assert dumped["mandateId"] == P.MANDATE_ID and "mandate_id" not in dumped and dumped["status"] == "active"
    assert dumped["maxAmount"] == "100"  # Decimal -> string, differs from the wire (100)
    receipt = M.AuthorizationReceipt.from_wire(P.RECEIPT)
    assert receipt.to_dict()["issuedAt"] == "2026-09-23T18:42:40.626000Z" != P.RECEIPT["issuedAt"]  # so re-submit .raw, never the dump
    assert receipt.raw == P.RECEIPT and receipt.raw["amount"] == "50.00"
    beneficiary = M.Beneficiary.from_wire(dict(P.BENEFICIARY, newField="future"))
    assert beneficiary.to_dict()["newField"] == "future"  # extra="allow"
    source = M.FundingSourceInput(type="sepa", destination=P.PAYER_IBAN, holder_name="Merchant B.V.")
    assert source.to_dict() == {"type": "sepa", "destination": P.PAYER_IBAN, "holderName": "Merchant B.V.", "label": None, "makeDefault": False}
    assert Amount == Decimal | int | float | str


def test_from_wire_refuses_bad_payloads() -> None:
    with pytest.raises(ResponseFormatError) as info:
        M.Payer.from_wire(["not", "an", "object"])  # type: ignore[arg-type]
    assert info.value.payload == ["not", "an", "object"] and info.value.error_code == "invalid_response"
    data = {"payoutId": "x"}
    with pytest.raises(ResponseFormatError) as info:
        M.Payout.from_wire(data)
    assert info.value.payload is data and "Payout" in str(info.value)
    with pytest.raises(ResponseFormatError):
        M.Payout.from_wire(dict(P.PAYOUT_PAID, amount="not-a-number"))


def test_lenient_enums() -> None:
    for model, data, attr, member in (
        (M.Payout, P.PAYOUT_PAID, "status", M.PayoutStatus.PAID),
        (M.Payer, P.PAYER_ACTIVE, "account_type", M.AccountType.BUSINESS),
        (M.Mandate, P.MANDATE, "scheme", M.MandateScheme.AGENT_PAYOUT),
        (M.Mandate, P.MANDATE_REVOKED, "status", M.MandateStatus.REVOKED),
        (M.AuthorizationReceipt, P.RECEIPT_REFUSED, "decision", M.Decision.REFUSED),
        (M.FundingSource, P.FUNDING_SOURCE, "type", M.FundingSourceType.SEPA),
    ):
        value = getattr(model.from_wire(data), attr)
        assert value is member and value == member.value, (model, attr)  # StrEnum compares equal to the plain string
    for model, data, alias, attr in (
        (M.Payout, P.PAYOUT_PAID, "status", "status"),
        (M.Payer, P.PAYER_ACTIVE, "accountType", "account_type"),
        (M.Mandate, P.MANDATE, "scheme", "scheme"),
        (M.PayoutExecution, P.EXECUTION_PAID, "status", "status"),
    ):
        instance = model.from_wire({**data, alias: "brand_new_value"})
        assert getattr(instance, attr) == "brand_new_value" and type(getattr(instance, attr)) is str, (model, attr)
        assert instance.to_dict()[alias] == "brand_new_value"
    assert M.CheckResult.from_wire({"name": "mandate_active", "passed": True, "detail": "ok"}).name is M.MandateCheck.MANDATE_ACTIVE
    assert M.CheckResult.from_wire({"name": "sanctions_screen", "passed": True, "detail": "ok"}).name == "sanctions_screen"
    assert M.Payout.from_wire({**P.PAYOUT_PAID, "status": M.PayoutStatus.PAID}).status is M.PayoutStatus.PAID


def test_enum_values_and_constants() -> None:
    expected = {
        M.AccountType: ["business", "individual"],
        M.PayerStatus: ["pending_verification", "active", "suspended"],
        M.FundingSourceType: ["sepa", "x402_wallet"],
        M.MandateScheme: ["sepa_core", "sepa_b2b", "agent_payout"],
        M.MandateType: ["one_off", "recurring"],
        M.Rail: ["sepa", "x402"],
        M.MandateStatus: ["active", "revoked", "expired"],
        M.Decision: ["approved", "refused"],
        M.PayoutStatus: ["draft", "pending_kyc", "kyc_rejected", "approved", "processing", "paid", "failed", "returned"],
        M.ProviderEvent: ["kyc_approved", "kyc_rejected", "payment_processing", "payment_paid", "payment_failed", "payment_returned"],
        M.MandateCheck: [c["name"] for c in P.CHECKS_PASSED],
    }
    for enum_cls, values in expected.items():
        assert issubclass(enum_cls, StrEnum) and [m.value for m in enum_cls] == values, enum_cls
    assert M.PayoutStatus.PAID == "paid" and f"{M.PayoutStatus.PAID}" == "paid"
    assert MANDATE_REFERENCE_MAX_LENGTH == 35 and MAX_MANDATE_DURATION_DAYS == 1095
    assert TERMINAL_PAYOUT_STATUSES == frozenset({"paid", "failed", "returned", "kyc_rejected"})


def test_money_is_decimal_and_timestamps_are_aware_utc() -> None:
    for model, data, attr, expected in (
        (M.Mandate, P.MANDATE, "max_amount", Decimal("100")),
        (M.Payout, P.PAYOUT_FAILED, "amount", Decimal("20.01")),
        (M.AuthorizationReceipt, P.RECEIPT, "amount", Decimal("50.00")),
        (M.AuthorizationReceipt, P.RECEIPT, "remaining_amount", Decimal("250")),
        (M.PayoutExecution, P.EXECUTION_PAID, "amount", Decimal("-50.00")),
        (M.PayoutExecution, P.EXECUTION_PAID, "balance_after", Decimal("950.00")),
        (M.MandateValidation, P.MANDATE_VALIDATION, "remaining_amount", Decimal("250")),
        (M.Simulation, P.SIMULATION, "opening_balance", Decimal("1000.00")),
    ):
        value = getattr(model.from_wire(data), attr)
        assert isinstance(value, Decimal) and value == expected and str(value) == str(expected), (model, attr)  # scale kept
    assert M.PayoutExecution.from_wire(P.EXECUTION_UNRESOLVED).balance_after is None  # null on the wire
    mandate = M.Mandate.from_wire(P.MANDATE_NO_PAYER)
    assert mandate.max_total_amount is None and mandate.payer_id is None  # absent on the wire
    assert M.Payout.from_wire({**P.PAYOUT_PAID, "amount": 0.1}).amount == Decimal("0.1")  # no binary noise
    assert M.MandateValidation.from_wire(P.MANDATE_VALIDATION).usage.total_amount == Decimal("0")
    assert M.X402PaymentPayload.from_wire(P.X402_PAYMENT_PAYLOAD).accepted.amount == "1.00"  # signed strings stay strings
    for model, data, attr, expected in (
        (M.Payer, P.PAYER_ACTIVE, "created_at", datetime(2026, 9, 23, 18, 42, 40, 612000, tzinfo=UTC)),
        (M.Mandate, P.MANDATE, "valid_until", datetime(2027, 9, 23, 18, 42, 40, 621000, tzinfo=UTC)),
        (M.AuthorizationReceipt, P.RECEIPT, "expires_at", datetime(2026, 9, 23, 18, 47, 40, 626000, tzinfo=UTC)),
        (M.Payout, P.PAYOUT_PAID, "executed_at", datetime(2026, 9, 23, 18, 42, 40, 733000, tzinfo=UTC)),
    ):
        value = getattr(model.from_wire(data), attr)
        assert value == expected and value.utcoffset() is not None and value.utcoffset().total_seconds() == 0, (model, attr)
    offset = M.Payout.from_wire({**P.PAYOUT_PAID, "createdAt": "2026-09-23T20:42:40.612+02:00"})
    assert offset.created_at == datetime(2026, 9, 23, 18, 42, 40, 612000, tzinfo=UTC)
    draft = M.Payout.from_wire(P.PAYOUT_DRAFT)
    assert draft.executed_at is None and draft.provider_reference is None
    assert all(e.timestamp.tzinfo is not None for e in M.Payout.from_wire(P.PAYOUT_PAID).history)


def test_model_properties() -> None:
    payer = M.Payer.from_wire(P.PAYER_ACTIVE)
    assert payer.default_funding_source is not None and payer.default_funding_source.source_id == P.SOURCE_ID
    sources = [{**P.FUNDING_SOURCE, "isDefault": False}, {**P.FUNDING_SOURCE_2, "isDefault": False}]
    assert M.Payer.from_wire({**P.PAYER_ACTIVE, "fundingSources": sources}).default_funding_source.source_id == P.SOURCE_ID  # type: ignore[union-attr]
    assert M.Payer.from_wire({**P.PAYER_ACTIVE, "fundingSources": []}).default_funding_source is None
    assert M.User.from_wire(P.USER).default_payment_method.method_id == P.METHOD_ID  # type: ignore[union-attr]
    assert M.User.from_wire({**P.USER, "paymentMethods": []}).default_payment_method is None
    assert M.AuthorizationReceipt.from_wire(P.RECEIPT).approved is True
    refused = M.AuthorizationReceipt.from_wire(P.RECEIPT_REFUSED)
    assert refused.approved is False and refused.failed_checks == ["per_payment_limit", "cumulative_limit"]
    for status in ("draft", "pending_kyc", "approved", "processing", "paid", "failed", "returned", "kyc_rejected", "brand_new_status"):
        assert M.Payout.from_wire({**P.PAYOUT_PAID, "status": status}).is_terminal is (status in TERMINAL_PAYOUT_STATUSES), status
    assert M.Payout.from_wire(P.PAYOUT_FAILED).last_note == P.PAYOUT_FAILED["history"][-1]["note"]
    assert M.Payout.from_wire({**P.PAYOUT_PAID, "history": []}).last_note is None
    history = [{"status": "draft", "timestamp": "2026-09-23T18:42:40.628Z", "note": "first"}, {"status": "pending_kyc", "timestamp": "2026-09-23T18:42:40.629Z"}]
    assert M.Payout.from_wire({**P.PAYOUT_DRAFT, "history": history}).last_note == "first"  # trailing blank notes are skipped
    assert M.PayoutExecution.from_wire(P.EXECUTION_PAID).unresolved is False  # absent on the wire
    assert M.PayoutExecution.from_wire(P.EXECUTION_UNRESOLVED).unresolved is True
    assert M.X402SettleResult.from_wire(P.X402_SETTLE_PENDING).pending is True
    for payload in (P.X402_SETTLE_SUCCESS, P.X402_SETTLE_MALFORMED, P.X402_SETTLE_NO_RAIL):
        assert M.X402SettleResult.from_wire(payload).pending is False
    assert M.X402SettleResult(success=True, error_reason="Settlement pending: later").pending is False
    assert M.X402SettleResult(success=False, error_reason="settlement pending").pending is False  # case-sensitive prefix
    assert M.X402Supported.from_wire(P.X402_SUPPORTED).can_settle is True
    assert M.X402Supported.from_wire(P.X402_SUPPORTED_EMPTY).can_settle is False
    auth = M.X402Authorization.from_wire(P.X402_PAYMENT_PAYLOAD["payload"]["authorization"])
    assert auth.from_ == P.PAYER_IBAN and auth.to_dict()["from"] == P.PAYER_IBAN and "from_" not in auth.to_dict()
    assert M.X402Quote.from_wire(P.X402_QUOTE_FREE).requirement is None
    assert M.X402Quote.from_wire(P.X402_QUOTE_PAID).requirement.pay_to == P.BENEFICIARY_IBAN  # type: ignore[union-attr]
    assert M.Health.from_wire({"status": "ok"}).payment_provider is None
    assert M.ToolDefinition.from_wire({"name": "execute_payout", "inputSchema": {}, "annotations": {"destructiveHint": True}}).destructive is True
    assert M.ToolDefinition.from_wire({"name": "get_payer", "inputSchema": {}}).destructive is False
    contents = M.ResourceContents.from_wire(P.MCP_RESOURCE_READ_RESULT)
    assert contents.contents[0].media_type == "application/json" and contents.json == P.PROVIDER_RESOURCE_JSON
    assert M.ResourceContent.from_wire({"uri": "x://y", "text": "plain", "mimeType": "text/plain"}).media_type == "text/plain"
    assert M.ResourceContents.from_wire({"contents": [{"uri": "x://y", "text": "not json"}]}).json is None
    assert M.ResourceContents.from_wire({"contents": []}).text is None
    message = M.PromptMessage.from_wire(P.MCP_PROMPT_GET_RESULT["messages"][0])
    assert message.role == "user" and message.text == message.content["text"]
    assert M.PromptMessage(role="user", content={"type": "image"}).text is None
    assert M.ServerInfo(name="agent-payouts-mcp", version="0.1.0", protocol_version="2025-06-18").capabilities == {}
    completion = M.Completion.from_wire(P.MCP_COMPLETION_RESULT["completion"])
    assert completion.values == [P.PAYOUT_ID] and completion.total == 1 and completion.has_more is False
