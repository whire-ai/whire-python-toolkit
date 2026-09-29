"""Service endpoints (health, capabilities), client plumbing and the keyword-only contract of every namespace method."""

from __future__ import annotations

import pytest

from tests.conftest import DEFAULT_API_KEY, RecordingTransport, json_response, make_client, ok
from tests.fixtures import payloads as P
from whire import Capabilities, Environment, Health, InvalidInputError, WhireClient, WhireError
from whire.models import MandateStatus, PayerStatus


async def test_health_and_capabilities(client: WhireClient, recording: RecordingTransport) -> None:
    recording.push(ok(P.HEALTH))
    health = await client.health()
    assert isinstance(health, Health) and health.raw == P.HEALTH
    assert (recording.last.method, recording.last.path, recording.last.body) == ("GET", "/api/health", b"")
    assert health.payment_provider is not None and health.payment_provider.simulated is True
    for fixture in (P.CAPABILITIES, P.CAPABILITIES_AUTH_ONLY, P.CAPABILITIES_PRODUCTION):
        recording.push(ok(fixture))
        capabilities = await client.capabilities()
        assert isinstance(capabilities, Capabilities) and capabilities.raw == fixture
        assert (recording.last.method, recording.last.path) == ("GET", "/api/capabilities")
        assert (capabilities.simulated, capabilities.environment) == (fixture["simulated"], fixture["environment"])
    recording.push(ok(P.HEALTH), ok(P.CAPABILITIES))
    async with make_client(recording, api_key=None) as anonymous:  # neither endpoint needs a key
        assert (await anonymous.health()).status == "ok" and (await anonymous.capabilities()).simulated is True
    assert "x-api-key" not in recording.last.headers and "authorization" not in recording.last.headers


def test_client_surface_and_environment() -> None:
    client = make_client()
    assert all(hasattr(client, name) for name in ("payers", "users", "beneficiaries", "mandates", "authorizations", "payouts", "simulation", "x402", "mcp"))
    assert client.base_url == "https://sandbox.whire.ai" and client.environment is Environment.SANDBOX and client.execute_timeout == 120.0
    assert client.mcp.endpoint == "https://sandbox.whire.ai/mcp"
    assert repr(client.mcp) == "WhireMCPClient(endpoint='https://sandbox.whire.ai/mcp', api_key='…abcd')"
    with pytest.raises(TypeError):
        WhireClient("key", "sandbox")  # type: ignore[misc]
    assert Environment.PRODUCTION.value == "https://api.whire.ai" and Environment.SANDBOX.value == "https://sandbox.whire.ai"
    assert Environment.parse("Production") is Environment.PRODUCTION and Environment.parse(Environment.SANDBOX) is Environment.SANDBOX
    with pytest.raises(InvalidInputError):
        Environment.parse("https://sandbox.whire.ai")


async def test_mcp_shares_the_http_client_and_closes_with_it(recording: RecordingTransport) -> None:
    recording.push(json_response({"jsonrpc": "2.0", "result": {}, "id": 1}))
    client = make_client(recording)
    await client.mcp.ping()
    assert recording.last.url == "https://sandbox.whire.ai/mcp" and recording.last.headers["x-api-key"] == DEFAULT_API_KEY
    await client.close()
    with pytest.raises(WhireError) as info:
        await client.mcp.list_tools()
    assert info.value.error_code == "client_closed" and len(recording) == 1


async def test_every_namespace_method_is_keyword_only(client: WhireClient, recording: RecordingTransport) -> None:
    """Positional parameters beyond the leading resource id, or that id passed by keyword, are TypeErrors before any request."""
    calls = [
        lambda c: c.payers.create("Merchant B.V."),
        lambda c: c.payers.activate(P.PAYER_ID, "Compliance Officer"),
        lambda c: c.payers.suspend(payer_id=P.PAYER_ID, reason="reason"),
        lambda c: c.payers.add_funding_source(P.PAYER_ID, "sepa", P.PAYER_IBAN_2),
        lambda c: c.payers.set_default_funding_source(P.PAYER_ID, P.SOURCE_ID),
        lambda c: c.payers.get(payer_id=P.PAYER_ID),
        lambda c: c.payers.list(PayerStatus.ACTIVE),
        lambda c: c.users.create("Smoke"),
        lambda c: c.users.add_payment_method(P.USER_ID, "sepa", P.USER_IBAN),
        lambda c: c.users.set_default_payment_method(user_id=P.USER_ID, method_id=P.METHOD_ID),
        lambda c: c.users.get(user_id=P.USER_ID),
        lambda c: c.users.list(10),
        lambda c: c.users.create_beneficiary(P.USER_ID, P.METHOD_ID),
        lambda c: c.beneficiaries.create("Acme Supplies BV", P.BENEFICIARY_IBAN, "acme"),
        lambda c: c.beneficiaries.get(beneficiary_id=P.BENEFICIARY_ID),
        lambda c: c.beneficiaries.validate_iban(P.BENEFICIARY_IBAN),
        lambda c: c.mandates.create(P.BENEFICIARY_ID, "SHOP-001", "Finance", 100),
        lambda c: c.mandates.validate(P.MANDATE_ID, 50),
        lambda c: c.mandates.revoke(mandate_id=P.MANDATE_ID),
        lambda c: c.mandates.get(),
        lambda c: c.mandates.list(MandateStatus.ACTIVE, P.PAYER_ID),
        lambda c: c.authorizations.create(P.MANDATE_ID, 50),
        lambda c: c.authorizations.verify(receipt=P.RECEIPT),
        lambda c: c.authorizations.verify(P.RECEIPT, "key"),
        lambda c: c.payouts.create(P.BENEFICIARY_ID, 50),
        lambda c: c.payouts.submit(payout_id=P.PAYOUT_ID),
        lambda c: c.payouts.record_event(P.PAYOUT_ID, "kyc_approved"),
        lambda c: c.payouts.execute(payout_id=P.PAYOUT_ID),
        lambda c: c.payouts.list("paid"),
        lambda c: c.payouts.wait(P.PAYOUT_ID, "paid"),
        lambda c: c.simulation.get(True),
        lambda c: c.simulation.reset(True),
        lambda c: c.x402.verify(P.X402_PAYMENT_PAYLOAD, P.X402_REQUIREMENTS),
        lambda c: c.x402.quote("http://example.test"),
        lambda c: c.x402.pay("http://example.test", P.MANDATE_ID),
        lambda c: c.health("extra"),
    ]
    for call in calls:
        with pytest.raises(TypeError):
            await call(client)  # type: ignore[misc]
    assert len(recording) == 0
