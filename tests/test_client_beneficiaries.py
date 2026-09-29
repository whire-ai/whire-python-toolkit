"""``client.beneficiaries.*`` - SPEC §3.1: ``create``/``validate_iban`` are REST, ``get`` has no REST route and uses MCP."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import RecordingTransport, ok, replayed, rpc_reply_for, tool_answer
from tests.fixtures import payloads as P
from whire import WhireClient
from whire._transport import serialize_body
from whire.exceptions import InvalidInputError
from whire.models import Beneficiary, IbanValidation

DOCS_CREATE_BODY: dict[str, Any] = {"fullName": "Acme Supplies BV", "iban": P.BENEFICIARY_IBAN, "reference": "acme", "email": "billing@acme.example"}


async def _create(client: WhireClient, **overrides: Any) -> Beneficiary:
    kwargs: dict[str, Any] = dict(full_name="Acme Supplies BV", iban=P.BENEFICIARY_IBAN, reference="acme", email="billing@acme.example")
    return await client.beneficiaries.create(**{**kwargs, **overrides})


async def test_create_sends_docs_body_exactly(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.BENEFICIARY), ok(P.BENEFICIARY), replayed(P.BENEFICIARY))
    beneficiary = await _create(client)
    assert recording.last.method == "POST" and recording.last.path == "/api/beneficiaries"
    assert recording.last.body == serialize_body(DOCS_CREATE_BODY)  # compact, declared order
    assert isinstance(beneficiary, Beneficiary) and beneficiary.raw == P.BENEFICIARY and beneficiary.replayed is False
    assert beneficiary.wallet_address is None  # accepted on create, not returned today
    assert "every time" in (client.beneficiaries.create.__doc__ or "")  # creates a new record every time
    await _create(client, email=None)
    assert recording.last.json == {"fullName": "Acme Supplies BV", "iban": P.BENEFICIARY_IBAN, "reference": "acme"}
    again = await _create(client, email=None, wallet_address="0xabc", idempotency_key="ben-1")
    assert recording.last.json["walletAddress"] == "0xabc" and recording.last.idempotency_key == "ben-1" and again.replayed is True
    for field in ("full_name", "iban", "reference"):
        with pytest.raises(InvalidInputError):
            await _create(client, **{field: ""})
    assert len(recording) == 3


async def test_get_goes_through_mcp(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(tool_answer(P.BENEFICIARY))
    beneficiary = await client.beneficiaries.get(P.BENEFICIARY_ID)
    assert recording.last.path == "/mcp"  # GET /api/beneficiaries/{id} does not exist (404)
    assert recording.last.tool_call == ("get_beneficiary", {"beneficiaryId": P.BENEFICIARY_ID})
    assert isinstance(beneficiary, Beneficiary) and beneficiary.raw == P.BENEFICIARY and beneficiary.replayed is False
    with pytest.raises(TypeError):
        await client.beneficiaries.get(P.BENEFICIARY_ID, idempotency_key="k")  # type: ignore[call-arg]
    with pytest.raises(InvalidInputError):
        await client.beneficiaries.get(" ")
    assert len(recording) == 1
    recording.push(lambda request: rpc_reply_for(request, {"content": [{"type": "text", "text": json.dumps(P.BENEFICIARY)}]}))
    fallback = await client.beneficiaries.get(P.BENEFICIARY_ID)  # text content when there is no structuredContent
    assert fallback.raw == P.BENEFICIARY


async def test_validate_iban(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.IBAN_VALIDATION), replayed(P.IBAN_VALIDATION_INVALID))
    result = await client.beneficiaries.validate_iban(iban="DE89 3704 0044 0532 0130 00")
    assert recording.last.method == "POST" and recording.last.path == "/api/validate-iban"
    assert recording.last.body == b'{"iban":"DE89 3704 0044 0532 0130 00"}' and recording.last.idempotency_key  # sent as given
    assert isinstance(result, IbanValidation) and result.is_valid is True and result.raw == P.IBAN_VALIDATION
    invalid = await client.beneficiaries.validate_iban(iban="NL91ABNA0417164301", country_code="NL", idempotency_key="iban-1")
    assert recording.last.json == {"iban": "NL91ABNA0417164301", "countryCode": "NL"} and recording.last.idempotency_key == "iban-1"
    assert invalid.is_valid is False and invalid.checksum_valid is False and invalid.replayed is True  # 200 even when invalid
    with pytest.raises(InvalidInputError):
        await client.beneficiaries.validate_iban(iban="")
    assert len(recording) == 2
