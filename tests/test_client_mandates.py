"""``client.mandates.*`` - SPEC §3.1 (Mandates): docs bodies, the local rules of ``create`` and client-side ``list`` filters."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import RecordingTransport, fail, ok, replayed
from tests.fixtures import payloads as P
from whire import WhireClient
from whire.exceptions import BadRequestError, InvalidInputError
from whire.models import MAX_MANDATE_DURATION_DAYS, Mandate, MandateCheck, MandateScheme, MandateStatus, MandateType, MandateValidation, Rail

DOCS_CREATE_BODY: dict[str, Any] = {
    "beneficiaryId": P.BENEFICIARY_ID,
    "payerId": P.PAYER_ID,
    "mandateReference": "SHOP-001",
    "signedBy": "Finance",
    "currency": "EUR",
    "maxAmount": 100,
    "maxTotalAmount": 250,
}
UTC = timezone.utc


async def _create(client: WhireClient, **overrides: Any) -> Mandate:
    kwargs: dict[str, Any] = dict(
        beneficiary_id=P.BENEFICIARY_ID, payer_id=P.PAYER_ID, mandate_reference="SHOP-001", signed_by="Finance", max_amount=100, max_total_amount=250
    )
    return await client.mandates.create(**{**kwargs, **overrides})


async def test_create_sends_docs_body_with_json_number_amounts(client: WhireClient, recording: RecordingTransport) -> None:
    recording.default = ok(P.MANDATE)
    mandate = await _create(client)
    assert recording.last.method == "POST" and recording.last.path == "/api/mandates" and recording.last.json == DOCS_CREATE_BODY
    assert isinstance(mandate, Mandate) and mandate.status is MandateStatus.ACTIVE and mandate.raw == P.MANDATE
    assert mandate.max_amount == Decimal("100") and mandate.scheme is MandateScheme.AGENT_PAYOUT
    await _create(client, max_amount=Decimal("100.00"), max_total_amount="250")
    assert b'"maxAmount":100,' in recording.last.body and b'"maxTotalAmount":250' in recording.last.body
    await _create(client, max_amount=Decimal("20.10"), max_total_amount=99.5)
    assert b'"maxAmount":20.1,' in recording.last.body and b'"maxTotalAmount":99.5' in recording.last.body
    await _create(client, max_amount="0.01", max_total_amount=None, currency="eur", idempotency_key="m-1")
    body = recording.last.json
    assert body["maxAmount"] == 0.01 and "maxTotalAmount" not in body and body["currency"] == "EUR"
    assert recording.last.idempotency_key == "m-1"
    for absent in ("fundingSourceId", "debtorName", "debtorIban", "scheme", "mandateType", "rail", "validFrom", "validUntil", "signedAt"):
        assert absent not in body
    sent = len(recording)
    for overrides in ({"max_amount": 0}, {"max_amount": "1.005"}, {"max_total_amount": "12.345"}, {"currency": "EURO"}, {"mandate_reference": "SHOP//1"}):
        with pytest.raises(InvalidInputError):
            await _create(client, **overrides)  # the shared matrices apply (test_validation.py)
    assert len(recording) == sent


async def test_create_payer_and_debtor_rules(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.MANDATE_NO_PAYER), ok(P.MANDATE), ok(P.MANDATE))
    mandate = await _create(client, payer_id=None, debtor_name="Merchant B.V.", debtor_iban=P.PAYER_IBAN)
    body = recording.last.json
    assert "payerId" not in body and body["debtorName"] == "Merchant B.V." and body["debtorIban"] == P.PAYER_IBAN
    assert mandate.payer_id is None and mandate.max_total_amount is None and mandate.raw == P.MANDATE_NO_PAYER
    await _create(client, debtor_name="Other Name", debtor_iban=P.PAYER_IBAN_2)  # both sent; the server uses the payer
    body = recording.last.json
    assert body["payerId"] == P.PAYER_ID and body["debtorName"] == "Other Name" and "payer" in (client.mandates.create.__doc__ or "")
    await _create(client, funding_source_id=P.SOURCE_ID_2)
    assert recording.last.json["fundingSourceId"] == P.SOURCE_ID_2
    for overrides in (
        {"payer_id": None},
        {"payer_id": None, "debtor_name": "Merchant B.V."},
        {"payer_id": None, "debtor_name": "", "debtor_iban": P.PAYER_IBAN},
        {"payer_id": P.PAYER_ID, "debtor_iban": P.PAYER_IBAN},  # a half debtor is refused even with a payer
    ):
        with pytest.raises(InvalidInputError):
            await _create(client, **overrides)
    assert len(recording) == 3


async def test_create_dates_and_validity_cap(client: WhireClient, recording: RecordingTransport) -> None:
    recording.default = ok(P.MANDATE)
    await _create(
        client,
        valid_from=datetime(2026, 9, 23, 18, 42, 40, 621000, tzinfo=UTC),
        valid_until=datetime(2027, 9, 23, 20, 42, 40, 621999, tzinfo=timezone(timedelta(hours=2))),
        signed_at=datetime(2026, 9, 23, 18, 42, 40, tzinfo=UTC),
    )
    body = recording.last.json
    assert (body["validFrom"], body["validUntil"], body["signedAt"]) == ("2026-09-23T18:42:40.621Z", "2027-09-23T18:42:40.621Z", "2026-09-23T18:42:40.000Z")
    await _create(client, valid_from=date(2026, 9, 23), valid_until="2027-09-23T18:42:40+00:00", signed_at="2026-09-23")
    body = recording.last.json
    assert (body["validFrom"], body["validUntil"], body["signedAt"]) == ("2026-09-23", "2027-09-23T18:42:40+00:00", "2026-09-23")  # as given
    start = datetime(2026, 1, 1, tzinfo=UTC)
    await _create(client, valid_from=start, valid_until=start + timedelta(days=MAX_MANDATE_DURATION_DAYS))  # exactly the cap
    await _create(client, valid_until=date(2040, 1, 1))  # a single bound is not capped
    sent = len(recording)
    for field in ("valid_from", "valid_until", "signed_at"):
        with pytest.raises(InvalidInputError):
            await _create(client, **{field: datetime(2026, 9, 23, 18, 42, 40)})  # naive
    with pytest.raises(InvalidInputError):
        await _create(client, valid_until="23/09/2026")
    for valid_from, valid_until in (
        (start, start + timedelta(days=MAX_MANDATE_DURATION_DAYS, hours=1)),  # the sandbox refuses 1095 days + 1 hour
        ("2026-01-01", "2030-01-01"),
        (date(2026, 9, 23), date(2026, 9, 22)),
    ):
        with pytest.raises(InvalidInputError):
            await _create(client, valid_from=valid_from, valid_until=valid_until)
    assert len(recording) == sent


async def test_create_scheme_type_and_rail(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.MANDATE), ok(P.MANDATE), fail("scheme must be one of sepa_core, sepa_b2b, agent_payout."))
    await _create(client, scheme=MandateScheme.SEPA_CORE, mandate_type=MandateType.ONE_OFF, rail=Rail.SEPA)
    body = recording.last.json
    assert (body["scheme"], body["mandateType"], body["rail"]) == ("sepa_core", "one_off", "sepa")
    await _create(client, scheme="agent_payout", mandate_type="recurring", rail="x402")
    assert (recording.last.json["scheme"], recording.last.json["rail"]) == ("agent_payout", "x402")
    with pytest.raises(BadRequestError):  # forward compatible: the server decides which schemes exist
        await _create(client, scheme="sepa_future")
    assert recording.last.json["scheme"] == "sepa_future"


async def test_validate(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.MANDATE_VALIDATION), ok(P.MANDATE_VALIDATION), replayed(P.MANDATE_VALIDATION_REFUSED))
    result = await client.mandates.validate(P.MANDATE_ID, amount=50, currency="EUR", beneficiary_id=P.BENEFICIARY_ID)
    assert recording.last.method == "POST" and recording.last.path == f"/api/mandates/{P.MANDATE_ID}/validate"
    assert recording.last.body == b'{"amount":50,"currency":"EUR","beneficiaryId":"' + P.BENEFICIARY_ID.encode() + b'"}'
    assert isinstance(result, MandateValidation) and result.is_valid is True and result.raw == P.MANDATE_VALIDATION
    assert [check.name for check in result.checks] == list(MandateCheck) and result.remaining_amount == Decimal("250")
    await client.mandates.validate(P.MANDATE_ID)
    assert recording.last.json == {}
    refused = await client.mandates.validate(P.MANDATE_ID, amount="20.10", currency="eur", payout_id=P.PAYOUT_ID, idempotency_key="val-1")
    assert recording.last.json == {"amount": 20.1, "currency": "EUR", "payoutId": P.PAYOUT_ID} and recording.last.idempotency_key == "val-1"
    assert refused.is_valid is False and refused.failed_checks == ["per_payment_limit", "cumulative_limit"] and refused.replayed  # not raised
    with pytest.raises(InvalidInputError):
        await client.mandates.validate(P.MANDATE_ID, amount="1.234")
    assert len(recording) == 3


async def test_revoke_and_get(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.MANDATE_REVOKED), replayed(P.MANDATE_REVOKED), ok(P.MANDATE), ok({**P.MANDATE, "status": "expired"}))
    mandate = await client.mandates.revoke(P.MANDATE_ID, reason="Withdrawn by finance")
    assert recording.last.method == "POST" and recording.last.path == f"/api/mandates/{P.MANDATE_ID}/revoke"
    assert recording.last.json == {"reason": "Withdrawn by finance"}
    assert mandate.status is MandateStatus.REVOKED and mandate.history[-1].note == "Withdrawn by finance" and mandate.raw == P.MANDATE_REVOKED
    again = await client.mandates.revoke(P.MANDATE_ID, idempotency_key="rev-1")
    assert recording.last.body == b"{}" and recording.last.idempotency_key == "rev-1" and again.replayed is True
    mandate = await client.mandates.get(P.MANDATE_ID)
    assert recording.last.method == "GET" and recording.last.path == f"/api/mandates/{P.MANDATE_ID}" and recording.last.body == b""
    assert mandate.raw == P.MANDATE and "expired" in (client.mandates.get.__doc__ or "")  # may record ``expired`` as a side effect
    assert (await client.mandates.get(P.MANDATE_ID)).status is MandateStatus.EXPIRED
    with pytest.raises(InvalidInputError):
        await client.mandates.revoke("")
    assert len(recording) == 4


async def test_list_filters_client_side(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(replayed(P.MANDATES_LIST))
    mandates = await client.mandates.list()
    assert recording.last.method == "GET" and recording.last.path == "/api/mandates" and recording.last.query == {}
    assert [m.mandate_id for m in mandates] == [P.MANDATE_ID] and mandates[0].raw == P.MANDATE and mandates[0].replayed is True
    revoked = {**P.MANDATE_REVOKED, "mandateId": "m-revoked"}
    other_payer = {**P.MANDATE, "mandateId": "m-other-payer", "payerId": "payer-2"}
    other_beneficiary = {**P.MANDATE, "mandateId": "m-other-ben", "beneficiaryId": "ben-2"}
    recording.default = ok({"mandates": [P.MANDATE, revoked, other_payer, other_beneficiary, P.MANDATE_NO_PAYER]})
    ids = lambda items: [m.mandate_id for m in items]  # noqa: E731
    assert ids(await client.mandates.list(status=MandateStatus.ACTIVE)) == [P.MANDATE_ID, "m-other-payer", "m-other-ben", P.MANDATE_NO_PAYER["mandateId"]]
    assert ids(await client.mandates.list(status="revoked")) == ["m-revoked"] and ids(await client.mandates.list(status="expired")) == []
    by_payer = await client.mandates.list(payer_id="payer-2")
    assert ids(by_payer) == ["m-other-payer"] and by_payer[0].raw == other_payer  # items keep their own raw
    assert ids(await client.mandates.list(beneficiary_id="ben-2")) == ["m-other-ben"]
    assert ids(await client.mandates.list(status="active", payer_id=P.PAYER_ID, beneficiary_id=P.BENEFICIARY_ID)) == [P.MANDATE_ID]
    assert recording.last.query == {}  # the server ignores every query param
    sent = len(recording)
    for status in ("ACTIVE", "cancelled"):
        with pytest.raises(InvalidInputError):
            await client.mandates.list(status=status)
    assert len(recording) == sent
