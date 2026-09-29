"""``client.payouts``: create, submit, record_event, confirm, execute, get, list, wait* (SPEC §3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tests.conftest import FakeClock, RecordingTransport, fail, make_client, ok, replayed, replayed_error
from tests.fixtures import payloads as P
from whire import AmbiguousResponseError, BadRequestError, InvalidInputError, NetworkError, NotFoundError, PayoutExecutionRefused, ServerError, WhireTimeoutError
from whire._transport import serialize_body
from whire.models import Payout, PayoutExecution, PayoutStatus, PayoutSubmission, PayoutTransition, ProviderEvent

EXECUTE_PATH = f"/api/payouts/{P.PAYOUT_ID}/execute"


@dataclass
class FakeRandom:
    """Replaces ``whire._resources.random``; ``factor`` is what ``uniform`` returns."""

    factor: float = 1.0
    calls: list[tuple[float, float]] = field(default_factory=list)

    def uniform(self, low: float, high: float) -> float:
        self.calls.append((low, high))
        return self.factor


@pytest.fixture
def no_jitter(monkeypatch: pytest.MonkeyPatch) -> FakeRandom:
    monkeypatch.setattr("whire._resources.random", fake := FakeRandom())
    return fake


# --------------------------------------------------------------------------- create / submit / events / confirm


async def test_create_sends_camel_case_body_and_parses_payout(client, recording: RecordingTransport) -> None:
    recording.push(ok(P.PAYOUT_DRAFT), ok(P.PAYOUT_NO_MANDATE), replayed(P.PAYOUT_DRAFT))
    payout = await client.payouts.create(beneficiary_id=P.BENEFICIARY_ID, amount=50, reason="Invoice 2026-091", mandate_id=P.MANDATE_ID)
    sent = recording.last
    assert (sent.method, sent.path) == ("POST", "/api/payouts") and sent.idempotency_key
    assert sent.body == serialize_body({"beneficiaryId": P.BENEFICIARY_ID, "mandateId": P.MANDATE_ID, "amount": 50, "currency": "EUR", "reason": "Invoice 2026-091"})
    assert isinstance(payout, Payout) and payout.status is PayoutStatus.DRAFT and payout.amount == Decimal("50") and payout.raw == P.PAYOUT_DRAFT
    payout = await client.payouts.create(beneficiary_id=P.BENEFICIARY_ID, amount="20.10")
    assert recording.last.json == {"beneficiaryId": P.BENEFICIARY_ID, "amount": 20.1, "currency": "EUR"}  # optionals omitted
    assert payout.mandate_id is None and payout.payout_id == P.PAYOUT_ID_NO_MANDATE
    payout = await client.payouts.create(beneficiary_id=P.BENEFICIARY_ID, amount=Decimal("20.10"), currency="eur", idempotency_key="my-key-1")
    assert recording.last.json["currency"] == "EUR" and recording.last.idempotency_key == "my-key-1" and payout.replayed is True
    for amount in (0, "20.123", True):
        with pytest.raises(InvalidInputError):
            await client.payouts.create(beneficiary_id=P.BENEFICIARY_ID, amount=amount)
    with pytest.raises(InvalidInputError):
        await client.payouts.create(beneficiary_id="", amount=50)
    assert len(recording) == 3


async def test_submit_record_event_and_confirm(client, recording: RecordingTransport) -> None:
    recording.push(ok(P.SUBMISSION), ok(P.TRANSITION_KYC_APPROVED), ok(P.TRANSITION_KYC_APPROVED), ok(P.TRANSITION_CONFIRMED))
    submission = await client.payouts.submit(P.PAYOUT_ID, idempotency_key="submit-key")
    sent = recording.last
    assert (sent.method, sent.path, sent.body, sent.idempotency_key) == ("POST", f"/api/payouts/{P.PAYOUT_ID}/submit", b"{}", "submit-key")
    assert isinstance(submission, PayoutSubmission) and submission.status is PayoutStatus.PENDING_KYC
    assert submission.next_action == P.SUBMISSION["nextAction"] and submission.raw == P.SUBMISSION
    for event in (ProviderEvent.KYC_APPROVED, "kyc_approved"):
        transition = await client.payouts.record_event(P.PAYOUT_ID_NO_MANDATE, event=event)
        sent = recording.last
        assert (sent.method, sent.path, sent.body) == ("POST", f"/api/payouts/{P.PAYOUT_ID_NO_MANDATE}/events", b'{"event":"kyc_approved"}')
        assert isinstance(transition, PayoutTransition) and transition.status is PayoutStatus.APPROVED and transition.raw == P.TRANSITION_KYC_APPROVED
    transition = await client.payouts.confirm(P.PAYOUT_ID)
    sent = recording.last
    assert (sent.method, sent.path, sent.body) == ("POST", f"/api/payouts/{P.PAYOUT_ID}/confirm", b"{}") and sent.idempotency_key
    assert transition.status is PayoutStatus.PROCESSING and transition.raw == P.TRANSITION_CONFIRMED
    with pytest.raises(InvalidInputError):
        await client.payouts.record_event(P.PAYOUT_ID, event="kyc_maybe")
    for method in ("submit", "confirm", "execute", "get"):
        for bad_id in ("", "..", "a/b", "../../api/reset?x=", "../health#"):  # never a path escape: refused locally
            with pytest.raises(InvalidInputError):
                await getattr(client.payouts, method)(bad_id)
    assert len(recording) == 4
    recording.push(fail(P.ERROR_EVENT_REFUSED, 400))
    with pytest.raises(BadRequestError) as info:
        await client.payouts.record_event(P.PAYOUT_ID, event="kyc_rejected")
    assert str(info.value) == P.ERROR_EVENT_REFUSED and not isinstance(info.value, PayoutExecutionRefused)


# --------------------------------------------------------------------------- execute


async def test_execute_success_uses_the_execute_timeout(recording: RecordingTransport) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = dict(request.extensions["timeout"])
        return ok(P.EXECUTION_PAID) if request.url.path.endswith("/execute") else ok(P.PAYOUT_APPROVED)

    recording.handler = handler
    async with make_client(recording, execute_timeout=77.0, timeout=12.0) as client:
        execution = await client.payouts.execute(P.PAYOUT_ID)
        await client.payouts.get(P.PAYOUT_ID)
    sent = recording.requests[0]
    assert (sent.method, sent.path, sent.body) == ("POST", EXECUTE_PATH, b"{}") and sent.idempotency_key
    assert isinstance(execution, PayoutExecution) and execution.status is PayoutStatus.PAID and execution.unresolved is False
    assert execution.provider_reference == "SIM-5DC8B5E2" and execution.amount == Decimal("-50.00") and execution.raw == P.EXECUTION_PAID
    assert seen[EXECUTE_PATH]["read"] == 77.0 and seen[f"/api/payouts/{P.PAYOUT_ID}"]["read"] == 12.0


async def test_execute_refusals(client, recording: RecordingTransport) -> None:
    recording.push(fail(P.ERROR_INSUFFICIENT_FUNDS, 400), ok(P.PAYOUT_FAILED))
    with pytest.raises(PayoutExecutionRefused) as info:
        await client.payouts.execute(P.PAYOUT_ID_FAILED)
    error = info.value
    assert str(error) == P.ERROR_INSUFFICIENT_FUNDS and error.error_code == "execution_refused" and error.status_code == 400
    assert error.needs_user_action and not error.is_input_error and not error.is_retryable
    assert error.idempotency_key == recording.requests[0].idempotency_key and "hand the decision to a human" in (error.suggestion or "")
    payout = await client.payouts.get(P.PAYOUT_ID_FAILED)  # the docstring's advice: the payout may now be failed
    assert payout.status is PayoutStatus.FAILED and payout.is_terminal is True
    recording.push(fail(P.ERROR_ALREADY_EXECUTED, 400), fail(P.ERROR_NOT_APPROVED, 400), fail(P.ERROR_PAYOUT_NOT_FOUND, 400))
    with pytest.raises(PayoutExecutionRefused) as already:
        await client.payouts.execute(P.PAYOUT_ID)
    assert already.value.error_code == "already_executed" and already.value.needs_user_action
    with pytest.raises(PayoutExecutionRefused) as not_approved:
        await client.payouts.execute(P.PAYOUT_ID)
    assert not_approved.value.error_code == "execution_refused"
    with pytest.raises(NotFoundError) as missing:
        await client.payouts.execute("nope")
    assert not isinstance(missing.value, PayoutExecutionRefused)


async def test_execute_unresolved_replays_and_network_semantics(client, retrying_client, recording: RecordingTransport, no_sleep) -> None:
    recording.push(ok(P.EXECUTION_UNRESOLVED), ok(P.EXECUTION_PAID), replayed(P.EXECUTION_PAID), replayed_error(P.ERROR_INSUFFICIENT_FUNDS, 400))
    execution = await client.payouts.execute(P.PAYOUT_ID_UNRESOLVED)
    assert execution.unresolved is True and execution.status is PayoutStatus.PROCESSING and execution.balance_after is None
    assert "Do not resend" in (execution.message or "")
    first = await client.payouts.execute(P.PAYOUT_ID, idempotency_key="exec-key")
    second = await client.payouts.execute(P.PAYOUT_ID, idempotency_key="exec-key")
    assert [r.idempotency_key for r in recording.requests[1:]] == ["exec-key", "exec-key"]
    assert first.replayed is False and second.replayed is True and second.provider_reference == first.provider_reference
    with pytest.raises(PayoutExecutionRefused):  # a replayed error is raised at once, never retried
        await retrying_client.payouts.execute(P.PAYOUT_ID, idempotency_key="exec-key")
    assert len(recording) == 4 and no_sleep == []
    recording.push(httpx.ConnectError("boom"))
    with pytest.raises(NetworkError) as info:
        await client.payouts.execute(P.PAYOUT_ID)
    assert not isinstance(info.value, AmbiguousResponseError) and info.value.is_retryable
    assert info.value.idempotency_key == recording.last.idempotency_key  # reuse it to retry safely
    recording.push(httpx.ReadTimeout("slow"), ok(P.EXECUTION_PAID))
    execution = await retrying_client.payouts.execute(P.PAYOUT_ID, idempotency_key="exec-key")  # keyed: safe to retry
    assert execution.status is PayoutStatus.PAID and len(recording) == 7 and len(no_sleep) == 1
    assert [(r.idempotency_key, r.body) for r in recording.requests[5:]] == [("exec-key", b"{}")] * 2
    unkeyed = RecordingTransport().push(httpx.ReadTimeout("slow"))
    async with make_client(unkeyed, auto_idempotency=False, max_retries=3) as plain:
        with pytest.raises(AmbiguousResponseError) as ambiguous:
            await plain.payouts.execute(P.PAYOUT_ID)
    assert len(unkeyed) == 1 and unkeyed.last.idempotency_key is None and ambiguous.value.needs_user_action


# --------------------------------------------------------------------------- get / list


async def test_get_and_list(client, recording: RecordingTransport) -> None:
    recording.push(ok(P.PAYOUT_PAID), ok(P.PAYOUT_LIST_PAID))
    payout = await client.payouts.get(P.PAYOUT_ID)
    sent = recording.last
    assert (sent.method, sent.path, sent.body, sent.idempotency_key) == ("GET", f"/api/payouts/{P.PAYOUT_ID}", b"", None)
    assert payout.status is PayoutStatus.PAID and payout.raw == P.PAYOUT_PAID
    assert [entry.status for entry in payout.history] == ["draft", "pending_kyc", "approved", "processing", "paid"]
    payouts = await client.payouts.list()
    assert (recording.last.method, recording.last.path, recording.last.query) == ("GET", "/api/payouts", {})
    assert [p.payout_id for p in payouts] == [P.PAYOUT_ID_UNRESOLVED, P.PAYOUT_ID] and payouts[0].raw == P.PAYOUT_LIST_PAID["payouts"][0]
    for status in ("paid", PayoutStatus.PAID):
        recording.push(ok(P.PAYOUT_LIST_PAID))
        await client.payouts.list(status=status)
        assert recording.last.query == {"status": "paid"}  # the only server-side filter
    sent_count = len(recording)
    for status in ("PAID", "settled", ""):
        with pytest.raises(InvalidInputError):
            await client.payouts.list(status=status)
    assert len(recording) == sent_count
    data = P.fresh(P.PAYOUT_LIST_PAID)
    data["payouts"][0]["mandateId"] = "mandate-2"
    data["payouts"][1]["beneficiaryId"] = "ben-2"
    recording.default = ok(data)
    assert [p.payout_id for p in await client.payouts.list(mandate_id="mandate-2")] == [P.PAYOUT_ID_UNRESOLVED] and recording.last.query == {}
    assert [p.payout_id for p in await client.payouts.list(beneficiary_id="ben-2")] == [P.PAYOUT_ID]
    assert await client.payouts.list(status="paid", mandate_id="mandate-2", beneficiary_id="ben-2") == [] and recording.last.query == {"status": "paid"}
    recording.default = ok({"payouts": [P.PAYOUT_NO_MANDATE, P.PAYOUT_DRAFT]})
    assert [p.payout_id for p in await client.payouts.list(mandate_id=P.MANDATE_ID)] == [P.PAYOUT_ID]  # no-mandate payouts drop out


# --------------------------------------------------------------------------- wait / wait_for_kyc / wait_for_settlement


async def test_wait_arguments(client, recording: RecordingTransport, no_sleep) -> None:
    for kwargs in ({}, {"status": "paid", "until": lambda p: True}, {"status": []}):
        with pytest.raises(ValueError):
            await client.payouts.wait(P.PAYOUT_ID, **kwargs)
    for status in ("settled", ["paid", "settled"]):
        with pytest.raises(InvalidInputError):
            await client.payouts.wait(P.PAYOUT_ID, status=status)
    assert len(recording) == 0
    recording.push(ok(P.PAYOUT_PENDING_KYC), ok(P.PAYOUT_APPROVED))
    payout = await client.payouts.wait(P.PAYOUT_ID, status="approved", timeout=30)  # a bare str is a one-element set
    assert payout.status is PayoutStatus.APPROVED and len(recording) == 2 and len(no_sleep) == 1
    assert all(r.method == "GET" and r.path == f"/api/payouts/{P.PAYOUT_ID}" for r in recording.requests)
    recording.push(ok(P.PAYOUT_PROCESSING), ok(P.PAYOUT_FAILED), ok(P.PAYOUT_PAID))
    assert (await client.payouts.wait(P.PAYOUT_ID, status=[PayoutStatus.PAID, "failed"])).status is PayoutStatus.FAILED
    assert (await client.payouts.wait(P.PAYOUT_ID, until=lambda p: p.is_terminal)).status is PayoutStatus.PAID
    assert len(recording) == 5 and len(no_sleep) == 2  # a satisfied predicate returns without sleeping


async def test_wait_interval_growth_clamp_and_jitter(client, recording: RecordingTransport, no_sleep, no_jitter, seeded_random) -> None:
    recording.push(ok(P.PAYOUT_PENDING_KYC), ok(P.PAYOUT_APPROVED))
    await client.payouts.wait(P.PAYOUT_ID, status="approved", interval=0.01)
    assert no_sleep == [0.25]  # clamped to a quarter second
    no_sleep.clear()
    recording.push(*[ok(P.PAYOUT_PENDING_KYC)] * 6, ok(P.PAYOUT_APPROVED))
    await client.payouts.wait(P.PAYOUT_ID, status="approved", timeout=1000, interval=1.0, max_interval=5.0)
    assert no_sleep == [1.0, 1.5, 2.25, 3.375, 5.0, 5.0]  # x1.5 per poll up to max_interval
    no_sleep.clear()
    no_jitter.calls.clear()
    no_jitter.factor = 0.8
    recording.push(ok(P.PAYOUT_PENDING_KYC), ok(P.PAYOUT_PENDING_KYC), ok(P.PAYOUT_APPROVED))
    await client.payouts.wait(P.PAYOUT_ID, status="approved", interval=2.0, max_interval=10.0)
    assert no_jitter.calls == [(0.8, 1.2), (0.8, 1.2)] and no_sleep == pytest.approx([1.6, 2.4])  # ±20 % jitter


async def test_wait_times_out_with_last_payout_and_elapsed(client, recording: RecordingTransport, no_sleep, no_jitter, clock: FakeClock) -> None:
    def poll(request: httpx.Request) -> httpx.Response:
        clock.now += 10.0
        return ok(P.PAYOUT_PENDING_KYC)

    recording.handler = poll
    with pytest.raises(WhireTimeoutError) as info:
        await client.payouts.wait(P.PAYOUT_ID, status="approved", timeout=25.0, interval=4.0, max_interval=10.0)
    error = info.value
    assert isinstance(error, TimeoutError) and error.error_code == "timeout" and error.payout_id == P.PAYOUT_ID
    assert isinstance(error.last, Payout) and error.last.status is PayoutStatus.PENDING_KYC and error.elapsed == pytest.approx(30.0)
    assert "last status: pending_kyc" in str(error) and P.PAYOUT_ID in str(error) and len(recording) == 3
    assert no_sleep == [4.0, 5.0]  # the second sleep is clamped to the remaining time rather than 4 x 1.5

    def failing(request: httpx.Request) -> httpx.Response:
        clock.now += 100.0
        return fail("boom", 503)

    recording.handler = failing
    with pytest.raises(WhireTimeoutError) as info:
        await client.payouts.wait(P.PAYOUT_ID, status="approved", timeout=50.0)
    assert info.value.last is None and "last status" not in str(info.value)


async def test_wait_error_handling(client, retrying_client, recording: RecordingTransport, no_sleep, clock: FakeClock) -> None:
    recording.push(fail(P.ERROR_PAYOUT_NOT_FOUND, 400))
    with pytest.raises(NotFoundError):  # non-retryable errors abort
        await client.payouts.wait("nope", status="approved")
    assert len(recording) == 1 and no_sleep == []
    recording.push(fail("upstream hiccup", 503), ok(P.PAYOUT_APPROVED))
    assert (await client.payouts.wait(P.PAYOUT_ID, status="approved")).status is PayoutStatus.APPROVED  # tolerated until the deadline
    assert len(recording) == 3 and len(no_sleep) == 1
    recording.push(fail("upstream hiccup", 503), ok(P.PAYOUT_APPROVED))
    assert (await retrying_client.payouts.wait(P.PAYOUT_ID, status="approved")).status is PayoutStatus.APPROVED
    assert len(recording) == 5 and len(no_sleep) == 2  # the transport's backoff, not a poll interval
    recording.push(httpx.ConnectError("down"), ok(P.PAYOUT_PAID))
    assert (await client.payouts.wait(P.PAYOUT_ID, status="paid")).status is PayoutStatus.PAID

    def poll(request: httpx.Request) -> httpx.Response:
        clock.now += 40.0
        return fail("down", 500)

    recording.handler = poll
    with pytest.raises(WhireTimeoutError):  # a retryable error past the deadline is a timeout ...
        await client.payouts.wait(P.PAYOUT_ID, status="paid", timeout=30.0)
    with pytest.raises(ServerError):  # ... while a plain read still raises it
        await client.payouts.get(P.PAYOUT_ID)


async def test_wait_for_kyc_and_settlement(client, recording: RecordingTransport, no_sleep, no_jitter, clock: FakeClock) -> None:
    recording.push(ok(P.PAYOUT_PENDING_KYC), ok(P.PAYOUT_PENDING_KYC), ok(P.PAYOUT_APPROVED), ok({**P.PAYOUT_PENDING_KYC, "status": "kyc_rejected"}))
    assert (await client.payouts.wait_for_kyc(P.PAYOUT_ID)).status is PayoutStatus.APPROVED and len(recording) == 3 and len(no_sleep) == 2
    rejected = await client.payouts.wait_for_kyc(P.PAYOUT_ID)
    assert rejected.status is PayoutStatus.KYC_REJECTED and rejected.is_terminal
    with pytest.raises(ValueError):
        await client.payouts.wait_for_kyc(P.PAYOUT_ID, status="approved")
    recording.push(ok(P.PAYOUT_PROCESSING), ok(P.PAYOUT_PAID), ok(P.PAYOUT_PROCESSING), ok(P.PAYOUT_FAILED), ok(P.PAYOUT_APPROVED))
    assert (await client.payouts.wait_for_settlement(P.PAYOUT_ID)).status is PayoutStatus.PAID
    assert (await client.payouts.wait_for_settlement(P.PAYOUT_ID_FAILED)).status is PayoutStatus.FAILED
    assert (await client.payouts.wait_for_settlement(P.PAYOUT_ID)).status is PayoutStatus.APPROVED  # not processing: no wait
    assert len(recording) == 9 and len(no_sleep) == 4
    no_sleep.clear()

    def poll(request: httpx.Request) -> httpx.Response:
        clock.now += 1.0
        return ok(P.PAYOUT_PROCESSING)

    recording.handler = poll
    with pytest.raises(WhireTimeoutError) as info:
        await client.payouts.wait_for_settlement(P.PAYOUT_ID, timeout=3.0, interval=0.5, max_interval=0.5)  # kwargs forwarded
    assert info.value.last is not None and info.value.last.status is PayoutStatus.PROCESSING and no_sleep == [0.5, 0.5]
