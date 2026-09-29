"""``client.x402``: the facilitator side (plain REST bodies) and the payer side (MCP tools) (SPEC §3.1)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.conftest import RecordingTransport, json_response, make_client, rpc_request, tool_answer, tool_failure, tool_reply_for
from tests.fixtures import payloads as P
from whire import (
    AmbiguousResponseError,
    BadRequestError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    PayoutExecutionRefused,
    X402Payment,
    X402PaymentPayload,
    X402PaymentRequirements,
    X402Quote,
    X402SettleResult,
    X402Supported,
    X402VerifyResult,
)
from whire._transport import serialize_body

URL = "http://127.0.0.1:4604/report"
BODY = {"payment_payload": P.X402_PAYMENT_PAYLOAD, "payment_requirements": P.X402_REQUIREMENTS}


async def test_supported_is_a_plain_body_get(client, recording: RecordingTransport) -> None:
    recording.push(json_response(P.X402_SUPPORTED), json_response(P.X402_SUPPORTED_EMPTY), json_response({"ok": True, "data": P.X402_SUPPORTED}))
    supported = await client.x402.supported()
    sent = recording.last
    assert (sent.method, sent.path, sent.body, sent.idempotency_key) == ("GET", "/x402/supported", b"", None)
    assert isinstance(supported, X402Supported) and supported.can_settle is True and supported.raw == P.X402_SUPPORTED
    assert supported.kinds[0].scheme == "sepa-mandate" and supported.kinds[0].extra["simulated"] is True
    assert (await client.x402.supported()).can_settle is False
    assert (await client.x402.supported()).kinds == []  # an {ok, data} envelope would be a different service


async def test_verify_sends_x402_version_and_untouched_payloads(client, recording: RecordingTransport) -> None:
    recording.default = json_response(P.X402_VERIFY_VALID)
    result = await client.x402.verify(**BODY)
    sent = recording.last
    assert (sent.method, sent.path) == ("POST", "/x402/verify") and sent.body == serialize_body(P.X402_VERIFY_REQUEST) and sent.idempotency_key
    assert list(sent.json) == ["x402Version", "paymentPayload", "paymentRequirements"] and sent.json["x402Version"] == 2
    assert sent.json["paymentPayload"]["payload"]["authorization"]["value"] == "1.00"  # signed strings stay strings
    assert isinstance(result, X402VerifyResult) and result.is_valid is True and result.payer == P.PAYER_IBAN and result.raw == P.X402_VERIFY_VALID
    payload = X402PaymentPayload.model_validate(P.X402_PAYMENT_PAYLOAD)
    requirements = X402PaymentRequirements.model_validate(P.X402_REQUIREMENTS)
    await client.x402.verify(payment_payload=payload, payment_requirements=requirements, idempotency_key="verify-1")  # local models are fine
    assert recording.last.json == P.X402_VERIFY_REQUEST and recording.last.idempotency_key == "verify-1"
    wire = {**P.X402_PAYMENT_PAYLOAD, "unknownServerField": {"keep": "me"}}
    await client.x402.verify(payment_payload=X402PaymentPayload.from_wire(wire), payment_requirements=P.X402_REQUIREMENTS)
    assert recording.last.json["paymentPayload"] == wire  # a model's raw wire object is preferred
    for reply in (P.X402_VERIFY_INVALID, P.X402_VERIFY_MALFORMED):
        recording.push(json_response(reply))
        result = await client.x402.verify(payment_payload={}, payment_requirements={})  # never raises on isValid:false
        assert result.is_valid is False and result.invalid_reason == reply["invalidReason"] and result.raw == reply
    sent_count = len(recording)
    for bad in (None, "payload", [P.X402_PAYMENT_PAYLOAD]):
        with pytest.raises(InvalidInputError):
            await client.x402.verify(payment_payload=bad, payment_requirements=P.X402_REQUIREMENTS)
    assert len(recording) == sent_count
    unkeyed = RecordingTransport().push(httpx.ReadTimeout("slow"))
    async with make_client(unkeyed, auto_idempotency=False, max_retries=3) as plain:
        with pytest.raises(AmbiguousResponseError):  # verify is an ordinary POST
            await plain.x402.verify(**BODY)
    assert len(unkeyed) == 1


async def test_settle_never_raises_and_is_retried_as_idempotent(recording: RecordingTransport, no_sleep) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = dict(request.extensions["timeout"])
        return json_response(P.X402_SETTLE_SUCCESS if request.url.path.endswith("settle") else P.X402_VERIFY_VALID)

    recording.handler = handler
    async with make_client(recording, execute_timeout=99.0, timeout=7.0) as client:
        result = await client.x402.settle(**BODY)
        await client.x402.verify(**BODY)
        sent = recording.requests[0]
        assert (sent.method, sent.path) == ("POST", "/x402/settle") and sent.body == serialize_body(P.X402_VERIFY_REQUEST)
        assert isinstance(result, X402SettleResult) and result.success and result.transaction == "SIM-37F5A5A9" and result.pending is False
        assert seen["/x402/settle"]["read"] == 99.0 and seen["/x402/verify"]["read"] == 7.0
        for reply, pending in ((P.X402_SETTLE_PENDING, True), (P.X402_SETTLE_MALFORMED, False), (P.X402_SETTLE_NO_RAIL, False)):
            recording.push(json_response(reply))
            result = await client.x402.settle(**BODY)
            assert result.success is False and result.error_reason == reply["errorReason"] and result.pending is pending and result.raw == reply
    recording.handler = None
    recording.push(httpx.ReadTimeout("slow"), json_response({"ok": False}, 502), json_response(P.X402_SETTLE_SUCCESS))
    async with make_client(recording, auto_idempotency=False, max_retries=3, retry_base_delay=0.01) as unkeyed:
        assert (await unkeyed.x402.settle(**BODY)).success is True  # idempotent per signed payload: retried even without a key
    assert len(recording) == 8 and all(r.idempotency_key is None for r in recording.requests[5:]) and len(no_sleep) == 2
    recording.push(*[httpx.ReadTimeout("slow")] * 4)
    async with make_client(recording, max_retries=3, retry_base_delay=0.01) as exhausted:
        with pytest.raises(NetworkError) as info:
            await exhausted.x402.settle(**BODY)
    assert not isinstance(info.value, AmbiguousResponseError) and len(recording) == 12


async def test_quote(client, retrying_client, recording: RecordingTransport, no_sleep) -> None:
    recording.push(tool_answer(P.X402_QUOTE_PAID), httpx.ReadTimeout("slow"), tool_answer(P.X402_QUOTE_FREE), tool_failure(P.MCP_ERROR_INVALID_ARGUMENTS))
    quote = await client.x402.quote(url=URL)
    assert recording.last.path == "/mcp" and recording.last.tool_call == ("quote_x402_resource", {"url": URL})
    assert isinstance(quote, X402Quote) and quote.free is False and quote.status == 402 and quote.raw == P.X402_QUOTE_PAID
    assert isinstance(quote.requirement, X402PaymentRequirements) and quote.requirement.amount == "1.00"
    free = await retrying_client.x402.quote(url="https://sandbox.whire.ai/api/health")  # read-only: retried
    assert free.free is True and free.requirement is None and len(recording) == 3 and len(no_sleep) == 1
    with pytest.raises(BadRequestError) as info:
        await client.x402.quote(url="not a url")
    assert info.value.error_code == "invalid_arguments"
    with pytest.raises(InvalidInputError):
        await client.x402.quote(url="")
    assert len(recording) == 4


async def test_pay_is_destructive_non_idempotent_and_translates_refusals(retrying_client, recording: RecordingTransport, no_sleep) -> None:
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        name = rpc_request(request)["params"]["name"]
        return tool_reply_for(request, P.X402_PAYMENT if name == "pay_x402_resource" else P.X402_QUOTE_FREE)

    async with make_client(handler, execute_timeout=88.0, timeout=9.0) as client:
        payment = await client.x402.pay(url=URL, mandate_id=P.MANDATE_ID, reason="Q3 report")
        await client.x402.quote(url=URL)
    assert isinstance(payment, X402Payment) and payment.paid is True and payment.payout_id == P.X402_PAYMENT["payoutId"] and payment.raw == P.X402_PAYMENT
    assert seen == [88.0, 9.0]  # pay uses the execute timeout
    recording.push(tool_answer(P.X402_PAYMENT))
    await retrying_client.x402.pay(url=URL, mandate_id=P.MANDATE_ID)
    assert recording.last.tool_call == ("pay_x402_resource", {"url": URL, "mandateId": P.MANDATE_ID})
    for answer in (httpx.ReadTimeout("slow"), json_response({"error": "boom"}, 500)):
        recording.push(answer)
        before = len(recording)
        with pytest.raises(AmbiguousResponseError) as ambiguous:
            await retrying_client.x402.pay(url=URL, mandate_id=P.MANDATE_ID)
        assert len(recording) == before + 1 and ambiguous.value.needs_user_action
    assert no_sleep == []
    recording.push(httpx.ConnectError("refused"), tool_answer(P.X402_PAYMENT))
    assert (await retrying_client.x402.pay(url=URL, mandate_id=P.MANDATE_ID)).paid is True  # never reached the server
    assert len(no_sleep) == 1
    for text, cls, code in (
        (P.MCP_ERROR_BUSINESS_EXECUTED, PayoutExecutionRefused, "already_executed"),
        ("Mandate X402-001 does not cover 1.00 EUR.", PayoutExecutionRefused, "execution_refused"),
        (P.ERROR_MANDATE_NOT_FOUND, NotFoundError, "not_found"),
        (P.MCP_ERROR_INVALID_ARGUMENTS, BadRequestError, "invalid_arguments"),
    ):
        recording.push(tool_failure(text))
        with pytest.raises(cls) as info:
            await retrying_client.x402.pay(url=URL, mandate_id=P.MANDATE_ID)
        assert type(info.value) is cls and info.value.error_code == code and info.value.status_code is None, text
    with pytest.raises(InvalidInputError):
        await retrying_client.x402.pay(url=URL, mandate_id="")
