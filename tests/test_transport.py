"""Transport layer: envelope, status mapping, retries, idempotency, auth and base-URL resolution (SPEC §3, §9)."""

from __future__ import annotations

import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from tests.conftest import (
    DEFAULT_API_KEY,
    RETRY_KW,
    RecordingTransport,
    fail,
    json_response,
    make_client,
    ok,
    replayed,
    replayed_error,
    text_response,
)
from tests.fixtures import payloads as P
from whire import (
    AmbiguousResponseError,
    AuthenticationError,
    BadRequestError,
    Environment,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ResponseFormatError,
    ServerError,
    WhireClient,
    WhireError,
    __version__,
)
from whire._transport import serialize_body

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FUNDING_SOURCE_INPUT = {"type": "sepa", "destination": P.PAYER_IBAN, "holderName": "Merchant B.V."}
X402_BODY = {"payment_payload": P.X402_PAYMENT_PAYLOAD, "payment_requirements": P.X402_REQUIREMENTS}


async def create_payer(client: WhireClient, **kwargs: Any):
    """A representative POST (``/api/payers``) for the retry tests."""
    fields: dict[str, Any] = dict(
        legal_name="Merchant B.V.",
        contact_first_name="Eva",
        contact_last_name="Jansen",
        email="finance@merchant.example",
        phone="+31612345678",
        funding_sources=[FUNDING_SOURCE_INPUT],
    )
    return await client.payers.create(**{**fields, **kwargs})


def _client(**kwargs: Any) -> WhireClient:
    kwargs.setdefault("transport", httpx.MockTransport(lambda r: ok(P.HEALTH)))
    kwargs.setdefault("api_key", DEFAULT_API_KEY)
    return WhireClient(**kwargs)


# --------------------------------------------------------------------------- envelope and status mapping


async def test_envelope_unwrap_and_malformed_bodies(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.HEALTH))
    health = await client.health()
    assert health.status == "ok" and health.raw == P.HEALTH and health.replayed is False
    assert recording.last.method == "GET" and recording.last.path == "/api/health"
    recording.push(fail(P.ERROR_AMOUNT_ZERO, 200))
    with pytest.raises(BadRequestError) as bad:  # ok:false on a 2xx
        await client.payouts.get(P.PAYOUT_ID)
    assert str(bad.value) == P.ERROR_AMOUNT_ZERO and bad.value.status_code == 200 and bad.value.is_input_error
    for body in ({"status": "ok"}, {"ok": True, "data": {"unexpected": True}}):
        recording.push(json_response(body))
        with pytest.raises(ResponseFormatError) as shape:
            await client.health()
        assert shape.value.error_code == "invalid_response" and shape.value.payload == body.get("data", body)


STATUS_TABLE: list[tuple[int, str, type[WhireError], str, bool]] = [
    (400, P.ERROR_PAYOUT_NOT_FOUND, NotFoundError, "not_found", True),
    (400, P.ERROR_AMOUNT_DECIMALS, BadRequestError, "bad_request", True),
    (401, "Unauthorized", AuthenticationError, "auth_failed", False),
    (403, "Forbidden", WhireError, "http_error", False),
    (404, P.ERROR_ROUTE_NOT_FOUND, NotFoundError, "route_not_found", False),
    (409, "Idempotency-Key in flight", IdempotencyConflictError, "idempotency_in_flight", False),
    (422, P.ERROR_IDEMPOTENCY_MISMATCH, IdempotencyMismatchError, "idempotency_mismatch", True),
    (429, "Too many requests", RateLimitError, "rate_limited", False),
    (500, "Internal error", ServerError, "server_error", False),
    (503, "Unavailable", ServerError, "server_error", False),
]


async def test_status_maps_to_exception_class() -> None:
    for status, message, cls, error_code, input_error in STATUS_TABLE:
        recording = RecordingTransport().push(fail(message, status, {"x-railway-request-id": "req-123"}))
        async with make_client(recording) as client:
            with pytest.raises(cls) as info:
                await client.payouts.get(P.PAYOUT_ID)
        error = info.value
        assert type(error) is cls and error.status_code == status and error.error_code == error_code, status
        assert error.is_input_error is input_error and error.request_id == "req-123"
        assert cls is AuthenticationError or str(error) == message
        assert len(recording) == 1
    recording = RecordingTransport().push(fail(P.ERROR_ROUTE_NOT_FOUND, 404), fail("Payout abc Not Found", 400))
    async with make_client(recording) as client:
        with pytest.raises(NotFoundError) as route:
            await client.payouts.get(P.PAYOUT_ID)
        assert route.value.error_code == "route_not_found" and "base_url" in (route.value.suggestion or "")
        with pytest.raises(NotFoundError) as record:  # the not-found sentence is matched case-insensitively
            await client.payouts.get("abc")
        assert record.value.error_code == "not_found"


async def test_non_json_bodies(recording: RecordingTransport, no_sleep: list[float]) -> None:
    recording.push(text_response("error code: 1010", 403), text_response("x" * 500, 403))
    async with make_client(recording, **RETRY_KW) as client:
        with pytest.raises(WhireError) as info:
            await client.health()
        assert type(info.value) is WhireError and info.value.status_code == 403 and info.value.error_code == "invalid_response"
        assert str(info.value) == "error code: 1010" and len(recording) == 1 and no_sleep == []  # 4xx text: no retry
        with pytest.raises(WhireError) as long:
            await client.health()
        assert len(str(long.value)) == 200  # truncated
        recording.push(*(text_response("<html>bad gateway</html>", 502) for _ in range(4)))
        with pytest.raises(ServerError) as server:  # 5xx text bodies are retryable ServerErrors
            await client.health()
        assert str(server.value) == "<html>bad gateway</html>" and server.value.is_retryable and len(no_sleep) == 3
        recording.push(text_response("<html>bad gateway</html>", 502), ok(P.HEALTH))
        assert (await client.health()).status == "ok"
        assert len(no_sleep) == 4
    recording.push(text_response("Unauthorized", 401))
    async with make_client(recording) as client:
        with pytest.raises(AuthenticationError):
            await client.health()


# --------------------------------------------------------------------------- retries: idempotent requests


async def test_idempotent_requests_retry_on_transient_failures(no_sleep: list[float]) -> None:
    answers: list[Any] = [fail("transient", status) for status in (409, 429, 500, 502, 503, 504)]
    answers += [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x"), httpx.ReadTimeout("x")]
    answers += [httpx.ReadError("x"), httpx.WriteError("x"), httpx.WriteTimeout("x"), httpx.RemoteProtocolError("x")]
    for answer in answers:
        recording = RecordingTransport().push(answer, ok(P.PAYOUT_PAID))
        async with make_client(recording, **RETRY_KW) as client:
            assert (await client.payouts.get(P.PAYOUT_ID)).status == "paid" and len(recording) == 2, answer
    assert len(no_sleep) == len(answers)


async def test_retries_are_exhausted_after_max_retries(retrying_client: WhireClient, recording: RecordingTransport, no_sleep: list[float]) -> None:
    recording.push(*(fail("down", 500) for _ in range(4)))
    with pytest.raises(ServerError) as server:
        await retrying_client.payouts.get(P.PAYOUT_ID)
    assert server.value.is_retryable and len(recording) == 4 and len(no_sleep) == 3  # first attempt + 3 retries
    recording.push(*(httpx.ConnectError("refused") for _ in range(4)))
    with pytest.raises(NetworkError) as network:
        await retrying_client.payouts.get(P.PAYOUT_ID)
    assert type(network.value) is NetworkError and network.value.is_retryable and "ConnectError" in str(network.value)
    recording.push(httpx.DecodingError("gzip"))  # other httpx errors: a non-retryable NetworkError
    with pytest.raises(NetworkError) as other:
        await retrying_client.payouts.get(P.PAYOUT_ID)
    assert not other.value.is_retryable and len(recording) == 9 and len(no_sleep) == 6


async def test_backoff_uses_full_jitter_and_is_capped(no_sleep: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("whire._transport.random", random.Random(1234))  # own generator: immune to global reseeding
    generator = random.Random(1234)
    expected = [generator.uniform(0, min(10.0, 0.5 * 2**attempt)) for attempt in range(3)]
    recording = RecordingTransport().push(fail("down", 500), fail("down", 502), fail("down", 503), ok(P.PAYOUT_PAID))
    async with make_client(recording, max_retries=3, retry_base_delay=0.5, retry_max_delay=10.0) as client:
        await client.payouts.get(P.PAYOUT_ID)
    assert no_sleep == pytest.approx(expected)
    no_sleep.clear()
    recording.push(*(fail("down", 500) for _ in range(3)), ok(P.PAYOUT_PAID))
    async with make_client(recording, max_retries=3, retry_base_delay=100.0, retry_max_delay=0.2) as client:
        await client.payouts.get(P.PAYOUT_ID)
    assert len(no_sleep) == 3 and all(0.0 <= delay <= 0.2 for delay in no_sleep)


async def test_no_retry_on_4xx(no_sleep: list[float]) -> None:
    for status, message, cls in (
        (400, P.ERROR_AMOUNT_ZERO, BadRequestError), (401, "Unauthorized", AuthenticationError), (403, "Forbidden", WhireError),
        (404, P.ERROR_ROUTE_NOT_FOUND, NotFoundError), (422, P.ERROR_IDEMPOTENCY_MISMATCH, IdempotencyMismatchError),
    ):
        recording = RecordingTransport().push(fail(message, status))
        async with make_client(recording, **RETRY_KW) as client:
            with pytest.raises(cls) as info:  # a keyed POST: 422 means the key was reused with other bytes
                await (client.payouts.submit(P.PAYOUT_ID, idempotency_key="k-1") if status == 422 else client.payouts.get(P.PAYOUT_ID))
        assert not info.value.is_retryable and len(recording) == 1, status
        assert status != 422 or info.value.idempotency_key == "k-1"
    assert no_sleep == []


async def test_replayed_error_stops_retrying(no_sleep: list[float]) -> None:
    for status, cls in ((409, IdempotencyConflictError), (500, ServerError), (429, RateLimitError), (400, BadRequestError)):
        recording = RecordingTransport().push(replayed_error("stored outcome", status))
        async with make_client(recording, **RETRY_KW) as client:
            with pytest.raises(cls) as info:
                await client.payouts.submit(P.PAYOUT_ID)
        assert str(info.value) == "stored outcome" and len(recording) == 1, status
    recording = RecordingTransport().push(replayed_error("stored", 503))  # also on an unkeyed POST
    async with make_client(recording, **RETRY_KW, auto_idempotency=False) as client:
        with pytest.raises(ServerError):
            await create_payer(client)
    assert len(recording) == 1 and no_sleep == []


# --------------------------------------------------------------------------- retries: non-idempotent requests


async def test_unkeyed_post_ambiguous_outcomes_are_not_retried(no_sleep: list[float]) -> None:
    answers: list[Any] = [httpx.ReadTimeout("x"), httpx.ReadError("x"), httpx.WriteError("x"), httpx.WriteTimeout("x")]
    answers += [httpx.RemoteProtocolError("x"), fail("down", 500), fail("down", 502), fail("boom", 504, {"x-railway-request-id": "req-504"})]
    for answer in answers:
        recording = RecordingTransport().push(answer)
        async with make_client(recording, **RETRY_KW, auto_idempotency=False) as client:
            with pytest.raises(AmbiguousResponseError) as info:
                await create_payer(client)
        error = info.value
        assert isinstance(error, NetworkError) and error.error_code == "ambiguous_outcome", answer
        assert not error.is_retryable and error.needs_user_action and error.idempotency_key is None
        assert len(recording) == 1  # exactly one request on the wire
    assert info.value.status_code == 504 and info.value.request_id == "req-504"
    recording = RecordingTransport().push(fail("in flight", 409))
    async with make_client(recording, **RETRY_KW, auto_idempotency=False) as client:
        with pytest.raises(IdempotencyConflictError):
            await create_payer(client)
    assert len(recording) == 1 and no_sleep == []


async def test_unkeyed_post_retries_only_when_the_request_never_reached_the_server(no_sleep: list[float]) -> None:
    answers: list[Any] = [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x"), fail("busy", 429), fail("down", 503)]
    for answer in answers:
        recording = RecordingTransport().push(answer, ok(P.PAYER_PENDING))
        async with make_client(recording, **RETRY_KW, auto_idempotency=False) as client:
            assert (await create_payer(client)).payer_id == P.PAYER_ID and len(recording) == 2, answer
        assert all(request.idempotency_key is None for request in recording.requests)
    assert len(no_sleep) == len(answers)


async def test_keyed_post_and_x402_settle_are_idempotent(retrying_client: WhireClient, recording: RecordingTransport, no_sleep: list[float]) -> None:
    recording.push(httpx.ReadTimeout("slow read"), ok(P.PAYER_PENDING))
    assert (await create_payer(retrying_client)).payer_id == P.PAYER_ID and len(recording) == 2 and len(no_sleep) == 1
    recording.push(*(httpx.ReadTimeout("slow read") for _ in range(4)))
    with pytest.raises(AmbiguousResponseError) as exhausted:  # may have landed: reuse the same key, never a fresh one
        await retrying_client.payouts.submit(P.PAYOUT_ID, idempotency_key="k-1")
    assert len(recording) == 6 and exhausted.value.idempotency_key == "k-1" and "'k-1'" in (exhausted.value.suggestion or "")
    settle = RecordingTransport().push(httpx.ReadTimeout("slow"), json_response(P.X402_SETTLE_SUCCESS))
    async with make_client(settle, **RETRY_KW, auto_idempotency=False) as client:  # idempotent per signed payload
        assert (await client.x402.settle(**X402_BODY)).success
    assert len(settle) == 2 and settle.last.idempotency_key is None


# --------------------------------------------------------------------------- idempotency keys and body bytes


async def test_auto_key_is_stable_and_body_bytes_identical_across_attempts(
    retrying_client: WhireClient, recording: RecordingTransport, no_sleep: list[float]
) -> None:
    recording.push(fail("down", 500), httpx.ConnectError("refused"), fail("busy", 409), ok(P.PAYER_PENDING))
    await create_payer(retrying_client)
    keys = {request.idempotency_key for request in recording.requests}
    assert len(recording) == 4 and len(keys) == 1 and UUID4_RE.match(keys.pop() or "")
    assert len({request.body for request in recording.requests}) == 1
    assert all(request.headers["content-type"] == "application/json" for request in recording.requests)
    recording.push(*(fail("in flight", 409) for _ in range(4)))
    with pytest.raises(IdempotencyConflictError) as info:  # the exhausted error carries the key to reuse
        await retrying_client.payouts.submit(P.PAYOUT_ID)
    key = recording.last.idempotency_key
    assert key and info.value.idempotency_key == key and key in (info.value.suggestion or "")


async def test_idempotency_key_rules(recording: RecordingTransport) -> None:
    recording.push(ok(P.SUBMISSION), ok(P.SUBMISSION), ok(P.SUBMISSION), ok(P.PAYOUT_PAID))
    async with make_client(recording) as client:
        await client.payouts.submit(P.PAYOUT_ID, idempotency_key="my-key-1")
        assert recording.last.idempotency_key == "my-key-1"  # the caller's key replaces the generated one
        await client.payouts.submit(P.PAYOUT_ID)
        await client.payouts.submit(P.PAYOUT_ID)
        first, second = (request.idempotency_key for request in recording.requests[1:3])
        assert first and second and first != second and uuid.UUID(first).version == 4  # one fresh key per call
        await client.payouts.get(P.PAYOUT_ID)
        assert "idempotency-key" not in recording.last.headers and recording.last.body == b""  # GET never carries a key
    recording.push(ok(P.SUBMISSION), ok(P.SUBMISSION))
    async with make_client(recording, auto_idempotency=False) as client:
        await client.payouts.submit(P.PAYOUT_ID)
        assert "idempotency-key" not in recording.last.headers
        await client.payouts.submit(P.PAYOUT_ID, idempotency_key="explicit")
        assert recording.last.idempotency_key == "explicit"
    recording.push(json_response(P.X402_VERIFY_VALID), json_response(P.X402_SETTLE_SUCCESS))
    async with make_client(recording) as client:
        for method in (client.x402.verify, client.x402.settle):  # x402 POSTs get an auto key too
            await method(**X402_BODY)
            assert UUID4_RE.match(recording.last.idempotency_key or "")
    assert [r.path for r in recording.requests[-2:]] == ["/x402/verify", "/x402/settle"]


async def test_body_is_serialized_compact_utf8(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.PAYER_PENDING))
    await create_payer(client, legal_name="Café Ünïcode B.V.")
    body = recording.last.body
    assert b"Caf\xc3\xa9" in body and b": " not in body and b", " not in body  # ensure_ascii=False, compact separators
    assert body == serialize_body(recording.last.json)
    assert serialize_body({"a": 1, "b": "é", "c": [1.5, None, True]}) == b'{"a":1,"b":"\xc3\xa9","c":[1.5,null,true]}'


async def test_replayed_and_raw_populated_on_models_and_list_items(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(replayed(P.PAYER_ACTIVE), replayed(P.PAYOUT_LIST_PAID), ok(P.PAYOUT_LIST_PAID))
    payer = await client.payers.get(P.PAYER_ID)
    assert payer.replayed is True and payer.raw == P.PAYER_ACTIVE and payer.raw is not P.PAYER_ACTIVE
    payouts = await client.payouts.list()
    for payout, item in zip(payouts, P.PAYOUT_LIST_PAID["payouts"], strict=True):
        assert payout.replayed is True and payout.raw == item
    assert all(p.replayed is False for p in await client.payouts.list())
    for value, expected in (("TRUE", True), (" true ", True), ("false", False)):
        recording.push(ok(P.PAYER_ACTIVE, headers={"idempotent-replay": value}))
        assert (await client.payers.get(P.PAYER_ID)).replayed is expected, value


# --------------------------------------------------------------------------- auth and base URL


async def test_auth_headers(recording: RecordingTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    recording.default = ok(P.HEALTH)
    async with make_client(recording) as client:
        await client.health()
    assert recording.last.headers["x-api-key"] == DEFAULT_API_KEY and "authorization" not in recording.last.headers
    async with make_client(recording, auth_scheme="bearer") as client:
        await client.health()
    assert recording.last.headers["authorization"] == f"Bearer {DEFAULT_API_KEY}" and "x-api-key" not in recording.last.headers
    with pytest.raises(InvalidInputError):
        make_client(auth_scheme="basic")  # type: ignore[arg-type]
    for key in (None, "", "   "):
        async with make_client(recording, api_key=key) as client:
            await client.health()
        assert "x-api-key" not in recording.last.headers and "authorization" not in recording.last.headers, key
    monkeypatch.setenv("WHIRE_API_KEY", "env-key-wxyz")
    async with make_client(recording, api_key=None) as client:
        await client.health()
    assert recording.last.headers["x-api-key"] == "env-key-wxyz"
    async with make_client(recording, api_key="arg-key-1234") as client:
        await client.health()
    assert recording.last.headers["x-api-key"] == "arg-key-1234"  # the argument wins over the environment
    monkeypatch.setenv("WHIRE_API_KEY", "   ")
    async with make_client(recording, api_key=None) as client:
        await client.health()
    assert "x-api-key" not in recording.last.headers  # a blank env key is no key


async def test_no_key_warns_once_and_a_key_never_warns(recording: RecordingTransport, caplog: pytest.LogCaptureFixture) -> None:
    recording.default = ok(P.HEALTH)
    with caplog.at_level(logging.WARNING, logger="whire"):
        async with make_client(recording, api_key=None) as client:
            await client.health()
            await client.health()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING and "no API key" in r.getMessage()]) == 1
        caplog.clear()
        async with make_client(recording) as client:
            await client.health()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_production_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for kwargs in ({"environment": Environment.PRODUCTION}, {"environment": "production"}, {"base_url": "https://api.whire.ai"}):
        with pytest.raises(AuthenticationError) as info:
            _client(api_key=None, **kwargs)
        assert "WHIRE_API_KEY" in str(info.value)
    client = _client(api_key=None, environment="production", allow_unauthenticated=True)
    assert client.base_url == "https://api.whire.ai" and client.environment is Environment.PRODUCTION
    assert _client(api_key=None).environment is Environment.SANDBOX  # the sandbox needs no key
    monkeypatch.setenv("WHIRE_API_KEY", "prod-key-9999")
    assert _client(api_key=None, environment="production").environment is Environment.PRODUCTION
    monkeypatch.setenv("WHIRE_API_KEY", "")
    monkeypatch.setenv("WHIRE_ENVIRONMENT", "production")
    with pytest.raises(AuthenticationError):
        _client(api_key=None)


async def test_base_url_resolution(recording: RecordingTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client().base_url == "https://sandbox.whire.ai" and _client().environment is Environment.SANDBOX
    for environment in ("sandbox", "SANDBOX", Environment.SANDBOX):
        assert _client(environment=environment).base_url == "https://sandbox.whire.ai"
    for environment in ("https://sandbox.whire.ai", "staging", ""):
        with pytest.raises(InvalidInputError):
            _client(environment=environment)
    client = _client(base_url="https://payments.example.com///", environment="production")
    assert client.base_url == "https://payments.example.com" and client.environment is None  # base_url wins; slashes stripped
    assert _client(base_url="https://sandbox.whire.ai/").environment is Environment.SANDBOX
    monkeypatch.setenv("WHIRE_BASE_URL", "https://payments.example.com/")
    monkeypatch.setenv("WHIRE_ENVIRONMENT", "production")
    assert _client(environment="sandbox").base_url == "https://sandbox.whire.ai"  # the argument wins over env vars
    assert _client().base_url == "https://payments.example.com"  # WHIRE_BASE_URL > WHIRE_ENVIRONMENT
    monkeypatch.delenv("WHIRE_BASE_URL")
    assert _client().environment is Environment.PRODUCTION
    monkeypatch.setenv("WHIRE_BASE_URL", "http://payments.example.com")
    with pytest.raises(InvalidInputError):
        _client()  # the env var is validated like the argument
    recording.push(ok(P.HEALTH))
    async with make_client(recording, base_url="https://payments.example.com/", environment=None) as client:
        await client.health()
    assert recording.last.url == "https://payments.example.com/api/health"


def test_plain_http_rules() -> None:
    for url in ("http://localhost:8080", "http://127.10.20.30", "http://[::1]:3000", "http://10.0.0.5", "http://172.16.4.2:9000", "http://192.168.1.20"):
        assert _client(base_url=url).base_url == url
    for url in ("http://payments.example.com", "http://sandbox.whire.ai", "http://8.8.8.8"):
        with pytest.raises(InvalidInputError, match="allow_insecure_http"):
            _client(base_url=url)
    assert _client(base_url="http://payments.example.com", allow_insecure_http=True).base_url == "http://payments.example.com"
    for url in ("ftp://payments.example.com", "sandbox.whire.ai", "https://", ""):
        with pytest.raises(InvalidInputError):
            _client(base_url=url)


async def test_close_semantics(recording: RecordingTransport) -> None:
    client = make_client(recording)
    await client.close()
    await client.close()  # idempotent
    assert client.is_closed
    for call in (client.health, client.mcp.ping):
        with pytest.raises(WhireError) as info:
            await call()
        assert info.value.error_code == "client_closed" and str(info.value) == "client is closed"
    assert len(recording) == 0
    async with make_client(recording) as managed:
        assert not managed.is_closed
    assert managed.is_closed


# --------------------------------------------------------------------------- Retry-After, request ids, settings


async def test_retry_after_is_honoured_up_to_the_cap(no_sleep: list[float]) -> None:
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=4), usegmt=True)
    for header, low, high in (("2", 2.0, 2.0), (when, 0.0, 4.0), ("0", 0.0, 0.0), ("soon", 0.0, 0.5)):  # unparseable -> plain backoff
        recording = RecordingTransport().push(fail("slow down", 429, {"Retry-After": header}), ok(P.PAYOUT_PAID))
        async with make_client(recording, max_retries=3, retry_base_delay=0.5, retry_max_delay=10.0) as client:
            await client.payouts.get(P.PAYOUT_ID)
        assert len(no_sleep) == 1 and low <= no_sleep[0] <= high, header
        no_sleep.clear()
    recording = RecordingTransport().push(fail("slow down", 429, {"Retry-After": "60"}))
    async with make_client(recording, max_retries=3, retry_max_delay=10.0) as client:
        with pytest.raises(RateLimitError) as info:  # beyond the cap: raise at once, no sleep
            await client.payouts.get(P.PAYOUT_ID)
    assert info.value.retry_after == 60.0 and info.value.is_retryable and len(recording) == 1
    recording = RecordingTransport().push(fail("maintenance", 503, {"Retry-After": "600"}))
    async with make_client(recording, max_retries=3, retry_max_delay=10.0) as client:
        with pytest.raises(ServerError):
            await client.payouts.get(P.PAYOUT_ID)
    recording = RecordingTransport().push(fail("slow down", 429))
    async with make_client(recording) as client:
        with pytest.raises(RateLimitError) as plain:
            await client.payouts.get(P.PAYOUT_ID)
    assert plain.value.retry_after is None and no_sleep == []


async def test_request_id_capture_order() -> None:
    for headers, expected in (
        ({"x-railway-request-id": "rw-1", "x-request-id": "xr-2", "cf-ray": "cf-3"}, "rw-1"),
        ({"x-request-id": "xr-2", "cf-ray": "cf-3"}, "xr-2"), ({"cf-ray": "cf-3"}, "cf-3"), ({}, None),
    ):
        recording = RecordingTransport().push(fail("down", 500, headers))
        async with make_client(recording) as client:
            with pytest.raises(ServerError) as info:
                await client.payouts.get(P.PAYOUT_ID)
        assert info.value.request_id == expected and info.value.to_agent_dict()["request_id"] == expected, headers


async def test_user_agent_timeouts_and_settings(recording: RecordingTransport) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = dict(request.extensions.get("timeout", {}))
        if request.url.path.endswith("/execute"):
            return ok(P.EXECUTION_PAID)
        return json_response(P.X402_SETTLE_SUCCESS) if request.url.path == "/x402/settle" else ok(P.PAYOUT_PAID)

    async with make_client(handler, timeout=7.0, execute_timeout=99.0) as client:
        await client.payouts.get(P.PAYOUT_ID)
        await client.payouts.execute(P.PAYOUT_ID)
        await client.x402.settle(**X402_BODY)
    assert seen[f"/api/payouts/{P.PAYOUT_ID}"] == {"connect": 5.0, "read": 7.0, "write": 10.0, "pool": 5.0}
    assert seen[f"/api/payouts/{P.PAYOUT_ID}/execute"]["read"] == 99.0 and seen["/x402/settle"]["read"] == 99.0
    async with make_client(handler, timeout=httpx.Timeout(3.0, connect=1.0)) as client:
        await client.payouts.get(P.PAYOUT_ID)
    assert seen[f"/api/payouts/{P.PAYOUT_ID}"] == {"connect": 1.0, "read": 3.0, "write": 3.0, "pool": 3.0}
    recording.push(ok(P.HEALTH), ok(P.HEALTH))
    async with make_client(recording) as client:
        await client.health()
    assert recording.last.headers["user-agent"] == f"whire-python/{__version__} httpx/{httpx.__version__}"
    async with make_client(recording, user_agent="acme-agent/1.0") as client:
        await client.health()
    assert recording.last.headers["user-agent"] == "acme-agent/1.0"
    for kwargs in ({"user_agent": " "}, {"max_retries": -1}, {"max_retries": 11}, {"retry_base_delay": -1}, {"execute_timeout": 0}):
        with pytest.raises(InvalidInputError):
            make_client(**kwargs)
    assert repr(make_client()) == "WhireClient(base_url='https://sandbox.whire.ai', api_key='…abcd')"
    assert repr(make_client(api_key=None)) == "WhireClient(base_url='https://sandbox.whire.ai', api_key=None)"
