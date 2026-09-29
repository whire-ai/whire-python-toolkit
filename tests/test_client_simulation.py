"""``client.simulation``: ``get()`` and the guarded ``reset()`` (SPEC §3.1)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests.conftest import RecordingTransport, fail, make_client, ok
from tests.fixtures import payloads as P
from whire import AmbiguousResponseError, Environment, InvalidInputError, ResetResult, ServerError, Simulation


async def test_get(client, recording: RecordingTransport) -> None:
    recording.push(ok(P.SIMULATION), ok(P.SIMULATION_DISABLED))
    simulation = await client.simulation.get()
    sent = recording.last
    assert (sent.method, sent.path, sent.body, sent.idempotency_key) == ("GET", "/api/simulation", b"", None)
    assert isinstance(simulation, Simulation) and simulation.enabled is True and simulation.raw == P.SIMULATION
    assert simulation.delay_ms == 3000 and simulation.opening_balance == Decimal("1000.00")
    assert simulation.triggers is not None and simulation.triggers.frozen_source_iban == P.FROZEN_SOURCE_IBAN
    unresolved = next(s for s in simulation.scenarios if s.trigger == "amount ending in .03")
    assert (unresolved.payout_status, unresolved.then) == ("processing", "paid") and simulation.scenarios[0].then is None
    assert simulation.accounts[1].status == "FROZEN" and simulation.accounts[0].balance == Decimal("929.97")
    disabled = await client.simulation.get()
    assert disabled.enabled is False and disabled.note == P.SIMULATION_DISABLED["note"] and disabled.triggers is None
    assert disabled.delay_ms is None and disabled.scenarios == [] and disabled.accounts == []


async def test_reset_is_guarded_by_capabilities_and_host(client, recording: RecordingTransport) -> None:
    recording.push(ok(P.CAPABILITIES), ok(P.RESET))
    result = await client.simulation.reset()
    assert [(r.method, r.path) for r in recording.requests] == [("GET", "/api/capabilities"), ("POST", "/api/reset")]
    assert recording.last.body == b"{}" and recording.last.idempotency_key
    assert isinstance(result, ResetResult) and result.payouts == [] and result.raw == P.RESET
    local = RecordingTransport().push(ok(P.CAPABILITIES), ok(P.RESET))
    async with make_client(local, base_url="http://localhost:3000", environment=None) as own_deployment:
        await own_deployment.simulation.reset()
    assert local.last.url == "http://localhost:3000/api/reset"
    for capabilities in (
        P.CAPABILITIES_AUTH_ONLY,
        P.CAPABILITIES_PRODUCTION,
        {**P.CAPABILITIES, "environment": "sandbox"},
        {**P.CAPABILITIES, "simulated": False},
    ):
        refused = RecordingTransport().push(ok(capabilities))
        async with make_client(refused) as guarded:
            with pytest.raises(InvalidInputError) as info:
                await guarded.simulation.reset()
        assert str(info.value) == "reset is only allowed against a simulated sandbox deployment"
        assert [r.path for r in refused.requests] == ["/api/capabilities"]
    for kwargs in ({"environment": Environment.PRODUCTION}, {"environment": None, "base_url": "https://api.whire.ai/"}):
        production = RecordingTransport()
        production.default = ok(P.CAPABILITIES)  # even when the capabilities claim a simulated sandbox
        async with make_client(production, **kwargs) as guarded:
            with pytest.raises(InvalidInputError, match="reset is only allowed"):
                await guarded.simulation.reset()
        assert all(r.path != "/api/reset" for r in production.requests)
    failing = RecordingTransport().push(fail("down", 500))
    async with make_client(failing) as guarded:
        with pytest.raises(ServerError):  # a failing capabilities read never reaches reset
            await guarded.simulation.reset()
    assert [r.path for r in failing.requests] == ["/api/capabilities"]


async def test_reset_force_skips_the_guards_and_follows_post_retry_rules(client, retrying_client, recording: RecordingTransport, no_sleep) -> None:
    recording.push(ok(P.RESET))
    assert (await client.simulation.reset(force=True)).payouts == []
    assert [(r.method, r.path) for r in recording.requests] == [("POST", "/api/reset")]
    production = RecordingTransport().push(ok(P.RESET))
    async with make_client(production, environment=Environment.PRODUCTION) as prod:
        await prod.simulation.reset(force=True)
    assert [r.path for r in production.requests] == ["/api/reset"]
    unkeyed = RecordingTransport().push(httpx.ReadTimeout("slow"))
    async with make_client(unkeyed, auto_idempotency=False, max_retries=3) as plain:
        with pytest.raises(AmbiguousResponseError):
            await plain.simulation.reset(force=True)
    assert len(unkeyed) == 1 and unkeyed.last.idempotency_key is None and no_sleep == []
    recording.push(ok(P.CAPABILITIES), fail("down", 500), ok(P.RESET))
    assert (await retrying_client.simulation.reset()).payouts == []  # keyed: retried like any keyed POST
    posts = [r for r in recording.requests if r.method == "POST"]
    assert len(posts) == 3 and posts[1].idempotency_key == posts[2].idempotency_key and len(no_sleep) == 1
