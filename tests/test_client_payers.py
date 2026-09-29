"""``client.payers.*`` - SPEC §3.1 (Payers): docs bodies, local checks, the MCP-only default source, client-side list filter."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import RecordingTransport, ok, replayed, tool_answer
from tests.fixtures import payloads as P
from whire import WhireClient
from whire.exceptions import InvalidInputError
from whire.models import AccountType, FundingSourceInput, FundingSourceType, Payer, PayerStatus

DOCS_CREATE_BODY: dict[str, Any] = {
    "legalName": "Merchant B.V.",
    "accountType": "business",
    "registrationNumber": "87654321",
    "vatNumber": "NL123456789B01",
    "contactFirstName": "Eva",
    "contactLastName": "Jansen",
    "email": "finance@merchant.example",
    "phone": "+31612345678",
    "fundingSources": [{"type": "sepa", "destination": P.PAYER_IBAN}],
}


async def _create(client: WhireClient, **overrides: Any) -> Payer:
    kwargs: dict[str, Any] = dict(
        legal_name="Merchant B.V.",
        account_type=AccountType.BUSINESS,
        registration_number="87654321",
        vat_number="NL123456789B01",
        contact_first_name="Eva",
        contact_last_name="Jansen",
        email="finance@merchant.example",
        phone="+31612345678",
        funding_sources=[{"type": "sepa", "destination": P.PAYER_IBAN}],
    )
    return await client.payers.create(**{**kwargs, **overrides})


async def test_create_sends_docs_body_exactly(client: WhireClient, recording: RecordingTransport) -> None:
    recording.default = ok(P.PAYER_PENDING)
    payer = await _create(client)
    assert recording.last.method == "POST" and recording.last.path == "/api/payers"
    assert recording.last.json == DOCS_CREATE_BODY  # ``makeDefault: false`` never goes on the wire
    assert isinstance(payer, Payer) and payer.status is PayerStatus.PENDING_VERIFICATION and payer.raw == P.PAYER_PENDING
    assert payer.default_funding_source is payer.funding_sources[0]
    source = FundingSourceInput(type=FundingSourceType.SEPA, destination=P.PAYER_IBAN, holder_name="Merchant B.V.", label="Main", make_default=True)
    await _create(client, account_type=None, registration_number=None, vat_number=None, funding_sources=[source])
    body = recording.last.json
    assert not {"accountType", "registrationNumber", "vatNumber"} & set(body)  # optionals omitted
    assert body["fundingSources"] == [{"type": "sepa", "destination": P.PAYER_IBAN, "holderName": "Merchant B.V.", "label": "Main", "makeDefault": True}]
    await _create(client, account_type="individual", idempotency_key="payer-1")
    assert recording.last.json["accountType"] == "individual" and recording.last.idempotency_key == "payer-1"
    for field in ("legal_name", "contact_first_name", "contact_last_name", "email", "phone"):
        with pytest.raises(InvalidInputError):
            await _create(client, **{field: "  "})
    for sources in ([], "NL91ABNA0417164300", [{"destination": P.PAYER_IBAN}], [42]):
        with pytest.raises(InvalidInputError):
            await _create(client, funding_sources=sources)
    assert len(recording) == 3


async def test_activate_suspend_and_add_funding_source(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.PAYER_ACTIVE), ok(P.PAYER_ACTIVE), ok(P.PAYER_SUSPENDED), ok(P.PAYER_ACTIVE), replayed(P.PAYER_ACTIVE))
    payer = await client.payers.activate(P.PAYER_ID, verified_by="Compliance Officer", note="KYB documents checked")
    assert recording.last.method == "POST" and recording.last.path == f"/api/payers/{P.PAYER_ID}/activate"
    assert recording.last.json == {"verifiedBy": "Compliance Officer", "note": "KYB documents checked"}
    assert payer.status is PayerStatus.ACTIVE and payer.raw == P.PAYER_ACTIVE
    await client.payers.activate(P.PAYER_ID, verified_by="Compliance Officer")
    assert recording.last.json == {"verifiedBy": "Compliance Officer"}  # note omitted
    assert "suspended" in (client.payers.activate.__doc__ or "")  # also reinstates a suspended payer
    suspended = await client.payers.suspend(P.PAYER_ID, reason="Chargeback investigation", idempotency_key="sus-1")
    assert recording.last.path == f"/api/payers/{P.PAYER_ID}/suspend" and recording.last.json == {"reason": "Chargeback investigation"}
    assert recording.last.idempotency_key == "sus-1" and suspended.status is PayerStatus.SUSPENDED
    await client.payers.add_funding_source(P.PAYER_ID, type="sepa", destination=P.PAYER_IBAN_2)
    assert recording.last.path == f"/api/payers/{P.PAYER_ID}/funding-sources"
    assert recording.last.json == {"type": "sepa", "destination": P.PAYER_IBAN_2}
    added = await client.payers.add_funding_source(
        P.PAYER_ID, type=FundingSourceType.SEPA, destination=P.PAYER_IBAN_2, holder_name="Merchant B.V.", label="Second", make_default=True, idempotency_key="fs-1"
    )
    assert recording.last.json == {"type": "sepa", "destination": P.PAYER_IBAN_2, "holderName": "Merchant B.V.", "label": "Second", "makeDefault": True}
    assert recording.last.idempotency_key == "fs-1" and added.replayed is True
    for call in (
        lambda: client.payers.activate(" ", verified_by="x"),
        lambda: client.payers.suspend(P.PAYER_ID, reason=""),
        lambda: client.payers.add_funding_source(P.PAYER_ID, type="sepa", destination=""),
    ):
        with pytest.raises(InvalidInputError):
            await call()
    assert len(recording) == 5


async def test_set_default_funding_source_goes_through_mcp(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(tool_answer(P.PAYER_ACTIVE))
    payer = await client.payers.set_default_funding_source(P.PAYER_ID, source_id=P.SOURCE_ID)
    assert recording.last.path == "/mcp" and recording.last.json["method"] == "tools/call"
    assert recording.last.tool_call == ("set_default_funding_source", {"payerId": P.PAYER_ID, "sourceId": P.SOURCE_ID})
    assert isinstance(payer, Payer) and payer.raw == P.PAYER_ACTIVE and payer.replayed is False
    with pytest.raises(TypeError):  # the server ignores Idempotency-Key on /mcp, so there is no parameter
        await client.payers.set_default_funding_source(P.PAYER_ID, source_id=P.SOURCE_ID, idempotency_key="k")
    with pytest.raises(InvalidInputError):
        await client.payers.set_default_funding_source(P.PAYER_ID, source_id="")
    assert len(recording) == 1


async def test_get_and_list_filters_status_client_side(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.PAYER_ACTIVE), replayed(P.PAYERS_LIST))
    payer = await client.payers.get(P.PAYER_ID)
    assert recording.last.method == "GET" and recording.last.path == f"/api/payers/{P.PAYER_ID}" and recording.last.body == b""
    assert payer.payer_id == P.PAYER_ID and payer.raw == P.PAYER_ACTIVE
    payers = await client.payers.list()
    assert recording.last.method == "GET" and recording.last.path == "/api/payers" and recording.last.query == {}
    assert [p.payer_id for p in payers] == [P.PAYER_ID] and payers[0].raw == P.PAYER_ACTIVE and payers[0].replayed is True
    pending = {**P.PAYER_PENDING, "payerId": "pending-1"}
    recording.default = ok({"payers": [P.PAYER_ACTIVE, pending, {**P.PAYER_SUSPENDED, "payerId": "suspended-1"}]})
    assert [p.payer_id for p in await client.payers.list()] == [P.PAYER_ID, "pending-1", "suspended-1"]
    assert [p.payer_id for p in await client.payers.list(status=PayerStatus.ACTIVE)] == [P.PAYER_ID]
    assert recording.last.query == {}  # the server ignores query params; filtering is local
    pending_only = await client.payers.list(status="pending_verification")
    assert [p.payer_id for p in pending_only] == ["pending-1"] and pending_only[0].raw == pending
    sent = len(recording)
    for status in ("ACTIVE", "frozen"):
        with pytest.raises(InvalidInputError):
            await client.payers.list(status=status)
    assert len(recording) == sent
