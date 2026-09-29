"""Opt-in end-to-end run against a live deployment (SPEC §9).

Run with::

    WHIRE_LIVE=1 .venv/bin/python -m pytest -q tests/live -p no:cacheprovider

Skipped entirely unless ``WHIRE_LIVE=1``. Targets ``WHIRE_BASE_URL`` when set,
else the sandbox. Every record is created with per-run unique data (see
``_data.py``); ``POST /api/reset`` is never called. Polling never goes below
one second between reads.

The scenarios are exactly SPEC §9's list. Tests that need the shared flow
(payer → beneficiary → mandate) read it from the module-scoped ``flow``
fixture; tests that need a payout from an earlier scenario read the
module-level ``STATE`` dict filled by that scenario.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from tests.live._data import RUN_ID, random_iban, unique_email, unique_reference, unique_text
from whire import (
    Beneficiary,
    InvalidInputError,
    Mandate,
    Payer,
    PayoutExecutionRefused,
    PayoutStatus,
    WhireClient,
)
from whire.tools import TOOLS

LIVE = os.environ.get("WHIRE_LIVE") == "1"
BASE_URL = os.environ.get("WHIRE_BASE_URL", "").strip() or None
API_KEY = os.environ.get("WHIRE_API_KEY", "").strip() or None

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(not LIVE, reason="live tests run only with WHIRE_LIVE=1"),
]

POLL = {"timeout": 45.0, "interval": 1.25, "max_interval": 3.0}
"""Polling settings: ``payouts.wait`` applies ±20 % jitter, so 1.25 s keeps every
sleep at or above one second on the shared sandbox."""

STATE: dict[str, Any] = {}
"""Ids created by earlier scenarios (``paid_payout_id``, ``failed_payout_id``, ...)."""


# --------------------------------------------------------------------------- fixtures


@dataclass
class Flow:
    """The shared records: an active payer, a beneficiary and an active mandate."""

    client: WhireClient
    simulated: bool
    payer_iban: str
    payer: Payer
    beneficiary: Beneficiary
    mandate: Mandate
    payout_ids: list[str] = field(default_factory=list)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client() -> AsyncIterator[WhireClient]:
    kwargs: dict[str, Any] = {"base_url": BASE_URL} if BASE_URL else {"environment": "sandbox"}
    async with WhireClient(API_KEY, **kwargs) as instance:
        yield instance


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def flow(client: WhireClient) -> Flow:
    """payer → activate → beneficiary → mandate, once per module."""
    capabilities = await client.capabilities()
    payer_iban = random_iban()
    # the generator's mod-97 check must agree with the server before the IBAN is used anywhere
    validation = await client.beneficiaries.validate_iban(iban=payer_iban)
    assert validation.is_valid is True, validation.raw
    assert validation.checksum_valid is True and validation.format_valid is True
    assert validation.normalized_iban == payer_iban
    payer = await client.payers.create(
        legal_name=f"Live Merchant {RUN_ID} B.V.",
        account_type="business",
        contact_first_name="Live",
        contact_last_name="Tester",
        email=unique_email("payer"),
        phone="+31612345678",
        funding_sources=[{"type": "sepa", "destination": payer_iban, "holder_name": "Live Merchant"}],
    )
    assert payer.status == "pending_verification"
    assert payer.raw is not None and payer.raw["payerId"] == payer.payer_id
    payer = await client.payers.activate(payer.payer_id, verified_by="Live Officer", note="live suite")
    assert payer.status == "active"
    assert payer.default_funding_source is not None
    assert payer.default_funding_source.identifier == payer_iban

    beneficiary = await client.beneficiaries.create(
        full_name=f"Live Supplies {RUN_ID}",
        iban=random_iban("DE", "37040044", 10),
        reference=unique_text("bene"),
        email=unique_email("bene"),
    )
    assert beneficiary.beneficiary_id and beneficiary.raw is not None

    mandate = await client.mandates.create(
        beneficiary_id=beneficiary.beneficiary_id,
        payer_id=payer.payer_id,
        mandate_reference=unique_reference(),
        signed_by="Finance",
        max_amount=100,
        max_total_amount=500,
    )
    assert mandate.status == "active"
    assert mandate.payer_id == payer.payer_id
    assert mandate.debtor_iban == payer_iban
    assert mandate.max_amount == Decimal(100)
    assert mandate.max_total_amount == Decimal(500)
    return Flow(client, capabilities.simulated, payer_iban, payer, beneficiary, mandate)


async def _approved_payout(flow: Flow, amount: str | int, reason: str) -> str:
    """Create, submit and wait for KYC; return the id of an ``approved`` payout."""
    payout = await flow.client.payouts.create(
        beneficiary_id=flow.beneficiary.beneficiary_id,
        mandate_id=flow.mandate.mandate_id,
        amount=amount,
        reason=f"{reason} {RUN_ID}",
    )
    assert payout.status == "draft"
    assert payout.amount == Decimal(str(amount))
    flow.payout_ids.append(payout.payout_id)
    submission = await flow.client.payouts.submit(payout.payout_id)
    assert submission.status == "pending_kyc", submission.raw
    assert submission.next_action
    approved = await flow.client.payouts.wait_for_kyc(payout.payout_id, **POLL)
    assert approved.status == "approved", approved.raw
    return payout.payout_id


def _needs_simulation(flow: Flow) -> None:
    if not flow.simulated:
        pytest.skip("cent-value triggers exist only when capabilities.simulated is true")


# --------------------------------------------------------------------------- 1. service


async def test_health_capabilities_and_x402_supported(client: WhireClient) -> None:
    health = await client.health()
    assert health.status == "ok"
    assert health.raw is not None and health.raw["status"] == "ok"
    assert health.payment_provider is not None and health.payment_provider.configured is True

    capabilities = await client.capabilities()
    assert capabilities.authorization is True
    assert capabilities.settlement is True
    assert isinstance(capabilities.simulated, bool)
    assert capabilities.environment
    assert health.payment_provider.simulated == capabilities.simulated

    supported = await client.x402.supported()
    assert supported.raw is not None
    assert isinstance(supported.kinds, list)
    for kind in supported.kinds:
        assert kind.scheme and kind.network and kind.x402_version >= 1


# --------------------------------------------------------------------------- 2. full flow


async def test_full_flow_verify_usable_and_direct_execute(flow: Flow) -> None:
    client = flow.client
    # payer / beneficiary / mandate were created by the fixture; re-read them
    payer = await client.payers.get(flow.payer.payer_id)
    assert payer.status == "active"
    mandate = await client.mandates.get(flow.mandate.mandate_id)
    assert mandate.status == "active" and mandate.signature

    validation = await client.mandates.validate(
        mandate.mandate_id, amount=50, currency="EUR", beneficiary_id=flow.beneficiary.beneficiary_id
    )
    assert validation.is_valid is True, validation.raw
    assert len(validation.checks) == 10
    assert all(check.passed for check in validation.checks)
    assert validation.failed_checks == []
    # Other live tests share this mandate and run in random order, so usage need not be zero here.
    assert validation.remaining_amount == Decimal(500) - validation.usage.total_amount

    receipt = await client.authorizations.create(
        mandate_id=mandate.mandate_id, amount=50, beneficiary_id=flow.beneficiary.beneficiary_id
    )
    assert receipt.approved is True, receipt.raw
    assert receipt.decision == "approved"
    assert receipt.amount == Decimal("50.00")
    assert receipt.raw is not None and receipt.raw["amount"] == "50.00"
    assert receipt.signature and receipt.failed_checks == []

    refused = await client.authorizations.create(
        mandate_id=mandate.mandate_id, amount=500, beneficiary_id=flow.beneficiary.beneficiary_id
    )
    assert refused.approved is False, refused.raw
    assert refused.decision == "refused"
    assert "per_payment_limit" in refused.failed_checks
    assert refused.signature

    verification = await client.authorizations.verify(receipt)
    assert verification.signature_valid is True, verification.raw
    assert verification.expired is False
    assert verification.usable is True
    assert verification.decision == "approved"

    tampered = deepcopy(receipt.raw)
    tampered["amount"] = "50.0"
    bad = await client.authorizations.verify(tampered)
    assert bad.signature_valid is False and bad.usable is False

    payout_id = await _approved_payout(flow, 50, "Full flow")
    execution = await client.payouts.execute(payout_id)
    assert execution.status == "paid", execution.raw
    assert execution.provider_reference
    assert execution.unresolved is False
    assert execution.amount == Decimal("-50.00")  # the debit, as the rail reports it
    assert execution.raw is not None and execution.raw["amount"] == "-50.00"
    assert execution.counterparty_iban == flow.beneficiary.iban
    assert execution.debited_account_iban == flow.payer_iban

    paid = await client.payouts.get(payout_id)
    assert paid.status == "paid" and paid.is_terminal
    assert paid.provider_reference == execution.provider_reference
    assert paid.executed_at is not None
    assert [entry.status for entry in paid.history][:1] == ["draft"]
    STATE["paid_payout_id"] = payout_id

    with pytest.raises(PayoutExecutionRefused) as excinfo:
        await client.payouts.execute(payout_id)
    assert excinfo.value.error_code == "already_executed"
    assert excinfo.value.needs_user_action is True
    assert "was already executed" in str(excinfo.value)

    usage = await client.mandates.validate(mandate.mandate_id, amount=1)
    assert usage.usage.payout_count >= 1
    assert usage.usage.total_amount >= Decimal(50)


# --------------------------------------------------------------------------- 3. confirm → execute


async def test_second_flow_confirm_then_execute(flow: Flow) -> None:
    client = flow.client
    payout_id = await _approved_payout(flow, 15, "Confirm flow")
    transition = await client.payouts.confirm(payout_id)
    assert transition.status == "processing", transition.raw
    assert transition.payout_id == payout_id

    processing = await client.payouts.get(payout_id)
    assert processing.status == "processing"
    assert processing.provider_reference is None  # confirm settles nothing

    execution = await client.payouts.execute(payout_id)
    assert execution.status == "paid", execution.raw
    assert execution.provider_reference
    paid = await client.payouts.get(payout_id)
    assert paid.status == "paid"
    STATE["confirmed_payout_id"] = payout_id


# --------------------------------------------------------------------------- 4. .01 → refused, failed


async def test_amount_01_refused_then_failed(flow: Flow) -> None:
    _needs_simulation(flow)
    client = flow.client
    payout_id = await _approved_payout(flow, "20.01", "AM04")
    with pytest.raises(PayoutExecutionRefused) as excinfo:
        await client.payouts.execute(payout_id)
    error = excinfo.value
    assert error.status_code == 400
    assert error.error_code == "execution_refused"
    assert "Insufficient funds" in str(error) or "AM04" in str(error), str(error)
    assert error.needs_user_action is True and error.is_retryable is False
    assert error.idempotency_key  # auto key, so a network retry could replay it

    failed = await client.payouts.get(payout_id)
    assert failed.status == "failed", failed.raw
    assert failed.is_terminal
    assert failed.last_note and ("AM04" in failed.last_note or "rejected" in failed.last_note.lower())
    STATE["failed_payout_id"] = payout_id


# --------------------------------------------------------------------------- 5. .03 → unresolved → paid


async def test_amount_03_unresolved_then_settles(flow: Flow) -> None:
    _needs_simulation(flow)
    client = flow.client
    payout_id = await _approved_payout(flow, "30.03", "Unresolved")
    execution = await client.payouts.execute(payout_id)
    assert execution.status == "processing", execution.raw
    assert execution.unresolved is True
    assert execution.raw is not None and execution.raw.get("unresolved") is True

    with pytest.raises(PayoutExecutionRefused) as excinfo:
        await client.payouts.execute(payout_id)  # refused until the rail reports
    assert "did not confirm" in str(excinfo.value) or "previous execution" in str(excinfo.value).lower()

    settled = await client.payouts.wait_for_settlement(payout_id, **POLL)
    assert settled.status == "paid", settled.raw
    assert settled.provider_reference
    STATE["unresolved_payout_id"] = payout_id


# --------------------------------------------------------------------------- 6. keyed execute replay


async def test_keyed_execute_replays(flow: Flow) -> None:
    client = flow.client
    payout_id = await _approved_payout(flow, 10, "Keyed")
    key = str(uuid.uuid4())
    first = await client.payouts.execute(payout_id, idempotency_key=key)
    assert first.status == "paid" and first.replayed is False
    second = await client.payouts.execute(payout_id, idempotency_key=key)
    assert second.replayed is True
    assert second.raw == first.raw
    assert second.provider_reference == first.provider_reference

    with pytest.raises(PayoutExecutionRefused) as excinfo:
        await client.payouts.execute(payout_id, idempotency_key=str(uuid.uuid4()))
    assert excinfo.value.error_code == "already_executed"
    STATE["keyed_payout_id"] = payout_id


# --------------------------------------------------------------------------- 7. x402 quote


async def test_x402_quote_on_free_url(client: WhireClient) -> None:
    url = f"{client.base_url}/api/health"
    quote = await client.x402.quote(url=url)
    assert quote.free is True, quote.raw
    assert quote.status == 200
    assert quote.url == url
    assert quote.requirement is None


# --------------------------------------------------------------------------- 8. users via MCP


async def test_users_flow_via_mcp(client: WhireClient) -> None:
    user_iban = random_iban()
    user = await client.users.create(
        first_name="Live",
        last_name=f"User{RUN_ID}",
        email=unique_email("user"),
        phone="+31698765432",
        payment_methods=[{"type": "sepa", "destination": user_iban, "holder_name": "Live User"}],
    )
    assert user.user_id and user.raw is not None
    assert user.default_payment_method is not None
    assert user.default_payment_method.destination == user_iban

    fetched = await client.users.get(user.user_id)
    assert fetched.user_id == user.user_id and fetched.email == user.email

    second_iban = random_iban()
    user = await client.users.add_payment_method(
        user.user_id, type="sepa", destination=second_iban, holder_name="Live User", label="second"
    )
    assert len(user.payment_methods) == 2
    added = next(method for method in user.payment_methods if method.destination == second_iban)
    assert added.is_default is False
    user = await client.users.set_default_payment_method(user.user_id, method_id=added.method_id)
    assert user.default_payment_method is not None
    assert user.default_payment_method.method_id == added.method_id

    users = await client.users.list()
    assert user.user_id in {item.user_id for item in users}
    assert all(item.raw is not None for item in users)

    beneficiary = await client.users.create_beneficiary(user.user_id)
    assert beneficiary.iban == second_iban  # the (new) default method
    assert beneficiary.beneficiary_id
    same = await client.beneficiaries.get(beneficiary.beneficiary_id)
    assert same.beneficiary_id == beneficiary.beneficiary_id
    assert same.iban == second_iban
    STATE["user_id"] = user.user_id


# --------------------------------------------------------------------------- 9/10. MCP tools


async def test_mcp_list_tools_is_33(client: WhireClient) -> None:
    info = await client.mcp.initialize()
    assert info.name and info.protocol_version
    tools = await client.mcp.list_tools()
    assert len(tools) == 33
    names = [tool.name for tool in tools]
    assert len(set(names)) == 33
    assert {tool.name for tool in tools if tool.destructive} == {"execute_payout", "pay_x402_resource"}
    for tool in tools:
        assert tool.input_schema.get("type") == "object", tool.name
        assert tool.output_schema, tool.name
    STATE["server_tools"] = [tool.raw for tool in tools]


def _strip_property_descriptions(schema: Any) -> Any:
    """Drop ``description`` inside ``properties`` entries (the one permitted local addition)."""
    if isinstance(schema, dict):
        result: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                result[key] = {
                    name: _strip_property_descriptions({k: v for k, v in prop.items() if k != "description"})
                    if isinstance(prop, dict)
                    else prop
                    for name, prop in value.items()
                }
            else:
                result[key] = _strip_property_descriptions(value)
        return result
    if isinstance(schema, list):
        return [_strip_property_descriptions(item) for item in schema]
    return schema


async def test_mcp_tools_parity_with_sdk_tools(client: WhireClient) -> None:
    local = TOOLS
    server = await client.mcp.list_tools()
    by_name = {tool.name: tool for tool in server}
    assert sorted(tool["name"] for tool in local) == sorted(by_name)
    for entry in local:
        tool = by_name[entry["name"]]
        assert entry.get("title") == tool.title, entry["name"]
        assert entry.get("description") == tool.description, entry["name"]
        assert _strip_property_descriptions(entry["input_schema"]) == tool.input_schema, entry["name"]
        assert entry.get("output_schema") == tool.output_schema, entry["name"]
        assert entry.get("annotations") == tool.annotations, entry["name"]
        assert entry.get("execution") == tool.execution, entry["name"]


# --------------------------------------------------------------------------- 11/12. lists


async def test_lists_contain_created_ids(flow: Flow) -> None:
    client = flow.client
    payers = await client.payers.list()
    assert flow.payer.payer_id in {payer.payer_id for payer in payers}
    active = await client.payers.list(status="active")
    assert flow.payer.payer_id in {payer.payer_id for payer in active}
    assert all(payer.status == "active" for payer in active)

    mandates = await client.mandates.list(payer_id=flow.payer.payer_id)
    assert [mandate.mandate_id for mandate in mandates] == [flow.mandate.mandate_id]
    all_mandates = await client.mandates.list()
    assert flow.mandate.mandate_id in {mandate.mandate_id for mandate in all_mandates}
    by_beneficiary = await client.mandates.list(beneficiary_id=flow.beneficiary.beneficiary_id)
    assert flow.mandate.mandate_id in {mandate.mandate_id for mandate in by_beneficiary}

    payouts = await client.payouts.list(mandate_id=flow.mandate.mandate_id)
    listed = {payout.payout_id for payout in payouts}
    assert set(flow.payout_ids) <= listed
    assert all(payout.raw is not None and payout.mandate_id == flow.mandate.mandate_id for payout in payouts)
    by_beneficiary_payouts = await client.payouts.list(beneficiary_id=flow.beneficiary.beneficiary_id)
    assert set(flow.payout_ids) <= {payout.payout_id for payout in by_beneficiary_payouts}

    if "user_id" in STATE:
        users = await client.users.list()
        assert STATE["user_id"] in {user.user_id for user in users}


async def test_payouts_list_by_status(flow: Flow) -> None:
    client = flow.client
    paid_ids = {STATE[key] for key in ("paid_payout_id", "confirmed_payout_id", "keyed_payout_id", "unresolved_payout_id") if key in STATE}
    assert paid_ids, "earlier scenarios recorded no paid payouts"
    paid = await client.payouts.list(status="paid", mandate_id=flow.mandate.mandate_id)
    assert paid_ids <= {payout.payout_id for payout in paid}
    assert all(payout.status == "paid" for payout in paid)

    paid_enum = await client.payouts.list(status=PayoutStatus.PAID, mandate_id=flow.mandate.mandate_id)
    assert {p.payout_id for p in paid_enum} == {p.payout_id for p in paid}

    if "failed_payout_id" in STATE:
        failed = await client.payouts.list(status="failed", mandate_id=flow.mandate.mandate_id)
        assert STATE["failed_payout_id"] in {payout.payout_id for payout in failed}
        assert STATE["failed_payout_id"] not in {payout.payout_id for payout in paid}
        assert all(payout.status == "failed" for payout in failed)

    drafts = await client.payouts.list(status="draft", mandate_id=flow.mandate.mandate_id)
    assert not (paid_ids & {payout.payout_id for payout in drafts})

    with pytest.raises(InvalidInputError):
        await client.payouts.list(status="PAID")
