"""``client.authorizations.*`` - SPEC §3.1: ``create`` posts the docs' body; ``verify`` must be byte-faithful to the receipt."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import RecordingTransport, fail, ok, replayed
from tests.fixtures import payloads as P
from whire import WhireClient
from whire._transport import serialize_body
from whire.exceptions import BadRequestError, InvalidInputError, NotFoundError
from whire.models import AuthorizationReceipt, Decision, MandateCheck, ReceiptVerification

DOCS_AUTHORIZE_BODY: dict[str, Any] = {"mandateId": P.MANDATE_ID, "amount": 50, "beneficiaryId": P.BENEFICIARY_ID}


async def _authorize(client: WhireClient, **overrides: Any) -> AuthorizationReceipt:
    kwargs: dict[str, Any] = dict(mandate_id=P.MANDATE_ID, amount=50, beneficiary_id=P.BENEFICIARY_ID)
    return await client.authorizations.create(**{**kwargs, **overrides})


async def test_create_sends_docs_body_with_a_json_number_amount(client: WhireClient, recording: RecordingTransport) -> None:
    recording.default = ok(P.RECEIPT)
    receipt = await _authorize(client)
    assert recording.last.method == "POST" and recording.last.path == "/api/authorize"
    assert recording.last.body == serialize_body(DOCS_AUTHORIZE_BODY)  # currency optional
    assert isinstance(receipt, AuthorizationReceipt) and receipt.approved and receipt.raw == P.RECEIPT and receipt.replayed is False
    assert receipt.amount == Decimal("50.00") and [check.name for check in receipt.checks] == list(MandateCheck)
    for amount, wire in (("50.00", b'"amount":50,'), (Decimal("20.10"), b'"amount":20.1,'), (0.1, b'"amount":0.1,')):
        await _authorize(client, amount=amount)
        assert wire in recording.last.body, amount  # never "50" nor 50.0
    await _authorize(client, currency="Usd", idempotency_key="auth-1")
    assert recording.last.json["currency"] == "USD" and recording.last.idempotency_key == "auth-1"
    await client.authorizations.create(mandate_id=P.MANDATE_ID, amount=50, payee=P.BENEFICIARY_IBAN, payee_name="Acme Supplies BV")
    assert recording.last.json == {"mandateId": P.MANDATE_ID, "amount": 50, "payee": P.BENEFICIARY_IBAN, "payeeName": "Acme Supplies BV"}
    await _authorize(client, payee=P.BENEFICIARY_IBAN)
    assert recording.last.json["beneficiaryId"] == P.BENEFICIARY_ID and recording.last.json["payee"] == P.BENEFICIARY_IBAN  # both are sent
    sent = len(recording)
    with pytest.raises(InvalidInputError, match="beneficiary_id or payee"):
        await client.authorizations.create(mandate_id=P.MANDATE_ID, amount=50, payee_name="Acme Supplies BV")
    for overrides in ({"amount": "50.123"}, {"currency": "EURO"}, {"mandate_id": ""}):
        with pytest.raises(InvalidInputError):
            await _authorize(client, **overrides)
    assert len(recording) == sent


async def test_create_returns_refusals_without_raising(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(replayed(P.RECEIPT_REFUSED), ok({**P.RECEIPT, "remainingAmount": None}), fail(P.ERROR_MANDATE_NOT_FOUND))
    refused = await _authorize(client, amount=500)
    assert refused.decision is Decision.REFUSED and refused.approved is False and refused.amount == Decimal("500.00")
    assert refused.failed_checks == ["per_payment_limit", "cumulative_limit"] and refused.raw == P.RECEIPT_REFUSED and refused.replayed
    assert [check.name for check in refused.checks if not check.passed] == [MandateCheck.PER_PAYMENT_LIMIT, MandateCheck.CUMULATIVE_LIMIT]
    assert (await _authorize(client)).remaining_amount is None
    with pytest.raises(NotFoundError):
        await _authorize(client, mandate_id="nope")


async def test_verify_sends_the_raw_receipt_byte_identical(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.RECEIPT), ok(P.RECEIPT_VERIFICATION), ok(P.RECEIPT_VERIFICATION), ok(P.RECEIPT_VERIFICATION))
    receipt = await _authorize(client)
    result = await client.authorizations.verify(receipt)
    assert recording.last.method == "POST" and recording.last.path == "/api/authorize/verify"
    body = recording.last.body
    assert body == serialize_body({"receipt": P.RECEIPT}) and isinstance(result, ReceiptVerification)
    assert body.startswith(b'{"receipt":{"receiptId":"' + P.RECEIPT_ID.encode())  # key order kept
    assert b'"amount":"50.00"' in body and b'"issuedAt":"2026-09-23T18:42:40.626Z"' in body  # never 50 / .626000Z / +00:00
    assert b'"fundsReserved":false' in body and b'"failedChecks":[]' in body and b'"remainingAmount":250' in body
    dumped = receipt.to_dict()  # a JSON-mode dump is NOT the wire object and never goes on the wire
    assert dumped["issuedAt"] != P.RECEIPT["issuedAt"] and dumped["remainingAmount"] != P.RECEIPT["remainingAmount"]
    assert body != serialize_body({"receipt": dumped})
    await client.authorizations.verify(AuthorizationReceipt.from_wire(receipt.raw))  # a model rebuilt from .raw is fine
    assert recording.last.body == body
    await client.authorizations.verify(P.fresh(P.RECEIPT))
    assert recording.last.body == body


async def test_verify_sends_a_mapping_untouched_and_refuses_local_models(client: WhireClient, recording: RecordingTransport) -> None:
    handed_over: dict[str, Any] = {  # key order, extra keys, odd types: all preserved byte for byte
        "signature": P.RECEIPT["signature"],
        "amount": "50.00",
        "remainingAmount": "250",
        "extra": {"nested": [1, 2.5, None, "ü"]},
        "issuedAt": "2026-09-23T18:42:40.626Z",
        "receiptId": P.RECEIPT_ID,
    }
    recording.push(ok(P.RECEIPT_VERIFICATION))
    await client.authorizations.verify(handed_over)
    assert recording.last.body == serialize_body({"receipt": handed_over}) and recording.last.body.startswith(b'{"receipt":{"signature":"')
    local = AuthorizationReceipt.model_validate(P.fresh(P.RECEIPT))
    assert local.raw is None
    with pytest.raises(InvalidInputError, match="exactly as received"):
        await client.authorizations.verify(local)
    for bad in ("auth_93c446d771524834b72915e906ed06b6", None, [P.RECEIPT]):
        with pytest.raises(InvalidInputError):
            await client.authorizations.verify(bad)  # type: ignore[arg-type]
    assert len(recording) == 1


async def test_verify_results_are_returned_not_raised(client: WhireClient, recording: RecordingTransport) -> None:
    expired = {**P.RECEIPT_VERIFICATION, "expired": True, "usable": False, "explanation": "Receipt has expired."}
    recording.push(replayed(P.RECEIPT_VERIFICATION), ok(P.RECEIPT_VERIFICATION_TAMPERED), ok(expired), fail("receipt must be an object."))
    result = await client.authorizations.verify(P.RECEIPT, idempotency_key="verify-1")
    assert recording.last.idempotency_key == "verify-1" and result.replayed is True and result.raw == P.RECEIPT_VERIFICATION
    assert result.signature_valid and result.usable and not result.expired and result.decision is Decision.APPROVED
    tampered = await client.authorizations.verify({**P.RECEIPT, "amount": "50.0"})
    assert b'"amount":"50.0"' in recording.last.body and tampered.signature_valid is False and tampered.usable is False
    stale = await client.authorizations.verify(P.RECEIPT)
    assert stale.signature_valid is True and stale.expired is True and stale.usable is False
    with pytest.raises(BadRequestError):
        await client.authorizations.verify({"receipt": "nope"})
