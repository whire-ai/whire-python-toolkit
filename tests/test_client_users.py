"""``client.users.*`` - SPEC §3.1 (Users): every method is an MCP ``tools/call`` with camelCase arguments."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import RecordingTransport, tool_answer, tool_failure
from tests.fixtures import payloads as P
from whire import WhireClient
from whire.exceptions import BadRequestError, InvalidInputError
from whire.models import Beneficiary, FundingSourceType, PaymentMethodInput, User


async def _create(client: WhireClient, **overrides: Any) -> User:
    kwargs: dict[str, Any] = dict(
        first_name="Smoke", last_name="User", email="user@example.com", phone="+31698765432", payment_methods=[{"type": "sepa", "destination": P.USER_IBAN}]
    )
    return await client.users.create(**{**kwargs, **overrides})


async def test_create_sends_register_user_arguments_exactly(client: WhireClient, recording: RecordingTransport) -> None:
    recording.handler = tool_answer(P.USER)
    user = await _create(client)
    assert recording.last.path == "/mcp" and recording.last.json["method"] == "tools/call"
    assert recording.last.tool_call == (
        "register_user",
        {"firstName": "Smoke", "lastName": "User", "email": "user@example.com", "phone": "+31698765432", "paymentMethods": [{"type": "sepa", "destination": P.USER_IBAN}]},
    )
    assert isinstance(user, User) and user.raw == P.USER and user.replayed is False
    assert user.default_payment_method is user.payment_methods[0]
    methods = [
        PaymentMethodInput(type=FundingSourceType.SEPA, destination=P.USER_IBAN, holder_name="Smoke User", make_default=True),
        {"type": "x402_wallet", "destination": "0xabc", "label": "Wallet"},
    ]
    await _create(client, payment_methods=methods)
    assert recording.last.tool_call[1]["paymentMethods"] == [
        {"type": "sepa", "destination": P.USER_IBAN, "holderName": "Smoke User", "makeDefault": True},
        {"type": "x402_wallet", "destination": "0xabc", "label": "Wallet"},
    ]
    sent = len(recording)
    for field in ("first_name", "last_name", "email", "phone"):
        with pytest.raises(InvalidInputError):
            await _create(client, **{field: ""})
    for methods in ([], P.USER_IBAN, [{"type": "sepa"}], [None]):
        with pytest.raises(InvalidInputError):
            await _create(client, payment_methods=methods)
    with pytest.raises(TypeError):  # no idempotency_key parameter: the server ignores the header on /mcp
        await _create(client, idempotency_key="k")
    assert len(recording) == sent


async def test_add_and_set_default_payment_method(client: WhireClient, recording: RecordingTransport) -> None:
    recording.handler = tool_answer(P.USER)
    user = await client.users.add_payment_method(P.USER_ID, type="sepa", destination=P.USER_IBAN)
    assert recording.last.tool_call == ("add_payment_method", {"userId": P.USER_ID, "type": "sepa", "destination": P.USER_IBAN})
    assert isinstance(user, User) and user.raw == P.USER
    await client.users.add_payment_method(
        P.USER_ID, type=FundingSourceType.X402_WALLET, destination="0xabc", holder_name="Smoke User", label="Wallet", make_default=True
    )
    assert recording.last.tool_call[1] == {
        "userId": P.USER_ID, "type": "x402_wallet", "destination": "0xabc", "holderName": "Smoke User", "label": "Wallet", "makeDefault": True
    }
    user = await client.users.set_default_payment_method(P.USER_ID, method_id=P.METHOD_ID)
    assert recording.last.tool_call == ("set_default_payment_method", {"userId": P.USER_ID, "methodId": P.METHOD_ID})
    assert user.default_payment_method is not None and user.default_payment_method.method_id == P.METHOD_ID
    sent = len(recording)
    for call in (
        lambda: client.users.add_payment_method("", type="sepa", destination=P.USER_IBAN),
        lambda: client.users.add_payment_method(P.USER_ID, type="sepa", destination=" "),
        lambda: client.users.set_default_payment_method(P.USER_ID, method_id=""),
    ):
        with pytest.raises(InvalidInputError):
            await call()
    assert len(recording) == sent


async def test_get_and_list(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(tool_answer(P.USER), tool_answer(P.USERS_LIST))
    user = await client.users.get(P.USER_ID)
    assert recording.last.tool_call == ("get_user", {"userId": P.USER_ID}) and user.raw == P.USER
    first_id = recording.last.json["id"]
    users = await client.users.list()
    assert recording.last.tool_call == ("list_users", {}) and b'"arguments":{}' in recording.last.body  # never omitted
    assert recording.last.json["id"] == first_id + 1  # ids increment across calls
    assert [u.user_id for u in users] == [P.USER_ID] and users[0].raw == P.USER and users[0].replayed is False
    other = {**P.USER, "userId": "user-2"}
    recording.push(tool_answer({"users": []}), tool_answer({"users": [P.USER, other, "junk", None]}))
    assert await client.users.list() == []
    users = await client.users.list()
    assert [u.user_id for u in users] == [P.USER_ID, "user-2"] and users[1].raw == other  # non-object items are skipped
    with pytest.raises(InvalidInputError):
        await client.users.get("")
    assert len(recording) == 4


async def test_create_beneficiary(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(tool_answer(P.BENEFICIARY_FOR_USER), tool_answer(P.BENEFICIARY_FOR_USER))
    beneficiary = await client.users.create_beneficiary(P.USER_ID)
    assert recording.last.tool_call == ("create_beneficiary_for_user", {"userId": P.USER_ID})
    assert isinstance(beneficiary, Beneficiary) and beneficiary.raw == P.BENEFICIARY_FOR_USER
    assert beneficiary.reference == f"user:{P.USER_ID}" and beneficiary.wallet_address is None
    await client.users.create_beneficiary(P.USER_ID, method_id=P.METHOD_ID)
    assert recording.last.tool_call == ("create_beneficiary_for_user", {"userId": P.USER_ID, "methodId": P.METHOD_ID})
    doc = client.users.create_beneficiary.__doc__ or ""
    assert "existing beneficiary" in doc and "x402_wallet" in doc
    recording.push(tool_failure("Payment method is an x402_wallet; this deployment settles over SEPA only."))
    with pytest.raises(BadRequestError) as info:
        await client.users.create_beneficiary(P.USER_ID, method_id=P.METHOD_ID)
    assert info.value.error_code == "bad_request" and info.value.status_code is None
