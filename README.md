# Whire Python SDK

[![PyPI version](https://img.shields.io/pypi/v/whire.svg)](https://pypi.org/project/whire/)
[![Python Versions](https://img.shields.io/pypi/pyversions/whire.svg)](https://pypi.org/project/whire/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**Payment infrastructure built specifically for AI agents.**

`whire` is the async Python client for the Whire **agent-payouts** API: payers, beneficiaries, signed mandates with
ten checks, signed authorization receipts, a payout lifecycle over a (simulated) SEPA rail, x402 over SEPA, and the
MCP server at `/mcp`. It ships an LLM tool-calling facade (`WhireToolkit`) over the same 33 tools the hosted MCP server
exposes, an MCP client that needs no `mcp` package, and a stdio MCP server for desktop clients.

The API reference lives at <https://sandbox.whire.ai/docs>. The public sandbox is `https://sandbox.whire.ai`; it runs
the simulated rail, so payouts settle against simulated money and no real money ever moves.

## Install

```bash
pip install whire
```

Python 3.11+. Runtime dependencies: `httpx` and `pydantic` v2. The package is typed (`py.typed`).

## Quickstart: the flow in five calls

Sign up a payer, record its verification, register who gets paid, sign a mandate, ask for a decision. Every method is
`async`; parameters are keyword-only except the leading record id; responses are pydantic models with snake_case
attributes (`payer.payer_id`), `Decimal` money and aware UTC datetimes.

The sandbox store is shared and a funding-source IBAN belongs to at most one payer (an email, too), so the examples
generate a throw-away IBAN per run instead of reusing the one in the docs.

```python
import asyncio
import random
import string
import uuid

from whire import WhireClient


def random_iban() -> str:
    """A syntactically valid throw-away NL IBAN (each payer IBAN can be registered once on the sandbox)."""
    bban = "ABNA" + "".join(random.choices(string.digits, k=10))
    check = 98 - int("".join(str(int(c, 36)) for c in bban + "NL00")) % 97
    return f"NL{check:02d}{bban}"


async def main() -> None:
    run = uuid.uuid4().hex[:8]
    async with WhireClient(environment="sandbox") as client:
        # 1. Sign up the payer; it opens pending_verification.
        payer = await client.payers.create(
            legal_name=f"Merchant {run} B.V.",
            account_type="business",
            contact_first_name="Eva",
            contact_last_name="Jansen",
            email=f"eva.{run}@example.com",
            phone="+31612345678",
            funding_sources=[{"type": "sepa", "destination": random_iban()}],
        )
        # 2. A person records the verification; the payer may now sign mandates.
        payer = await client.payers.activate(payer.payer_id, verified_by="Compliance Officer")
        print(payer.status)  # active

        # 3. Register who gets paid (a new record every time; no dedup).
        payee = await client.beneficiaries.create(
            full_name="Acme Supplies BV", iban="DE89370400440532013000", reference=f"acme-{run}"
        )

        # 4. The payer signs a mandate for that payee: 100 EUR per payment, 250 EUR in total.
        mandate = await client.mandates.create(
            beneficiary_id=payee.beneficiary_id,
            payer_id=payer.payer_id,
            mandate_reference=f"SHOP-{run}",
            signed_by="Finance",
            max_amount=100,
            max_total_amount=250,
        )
        print(mandate.status, mandate.debtor_iban)  # active NL..

        # 5. May this payment proceed? A signed receipt says yes or no and why.
        receipt = await client.authorizations.create(
            mandate_id=mandate.mandate_id, amount=50, beneficiary_id=payee.beneficiary_id
        )
        print(receipt.decision, receipt.failed_checks)  # approved []

        refused = await client.authorizations.create(
            mandate_id=mandate.mandate_id, amount=150, beneficiary_id=payee.beneficiary_id
        )
        print(refused.decision, refused.failed_checks)  # refused ['per_payment_limit']

        # Anyone can check a receipt. It is verified byte-for-byte, so pass it exactly as received.
        verification = await client.authorizations.verify(receipt)
        print(verification.signature_valid, verification.usable)  # True True


asyncio.run(main())
```

A mandate can also be signed without a payer by giving `debtor_name=` and `debtor_iban=` (when both a `payer_id` and
a debtor are sent, the server uses the payer). On the simulated rail every debtor IBAN is an account that opens with
1000.00 EUR.

## The payout lifecycle

```
draft → pending_kyc → approved → processing → paid
                   ↘ kyc_rejected        ↘ failed      paid → returned
```

`create` makes a draft under a mandate, `submit` sends it to KYC review, and on a deployment with a rail the KYC
decision arrives by itself (about 3 s on the sandbox) — poll with `wait_for_kyc`. `execute` works straight from
`approved`; `confirm` (a human confirmation that moves `approved` → `processing` and settles nothing) is optional.
There are no webhooks: poll.

```python
import asyncio
import random
import string
import uuid

from whire import WhireClient


def random_iban() -> str:
    bban = "ABNA" + "".join(random.choices(string.digits, k=10))
    check = 98 - int("".join(str(int(c, 36)) for c in bban + "NL00")) % 97
    return f"NL{check:02d}{bban}"


async def main() -> None:
    run = uuid.uuid4().hex[:8]
    async with WhireClient(environment="sandbox") as client:
        payee = await client.beneficiaries.create(
            full_name="Acme Supplies BV", iban="DE89370400440532013000", reference=f"acme-{run}"
        )
        mandate = await client.mandates.create(
            beneficiary_id=payee.beneficiary_id,
            debtor_name=f"Merchant {run} B.V.",
            debtor_iban=random_iban(),
            mandate_reference=f"PAY-{run}",
            signed_by="Finance",
            max_amount=100,
            max_total_amount=250,
        )

        payout = await client.payouts.create(
            beneficiary_id=payee.beneficiary_id,
            mandate_id=mandate.mandate_id,
            amount="20.00",  # Decimal | int | float | str; positive, at most two decimals
            reason="Invoice 2026-091",
        )
        submission = await client.payouts.submit(payout.payout_id)
        print(submission.status, "->", submission.next_action)  # pending_kyc -> The provider answers on its own; poll ...

        payout = await client.payouts.wait_for_kyc(payout.payout_id, timeout=30)
        if payout.status == "kyc_rejected":  # terminal: a new draft is needed
            raise SystemExit(payout.last_note)
        print(payout.status)  # approved

        # Optional human step; it moves nothing.
        await client.payouts.confirm(payout.payout_id)

        execution = await client.payouts.execute(payout.payout_id)
        print(execution.status, execution.provider_reference, execution.unresolved)  # paid SIM-... False

        if execution.status == "processing":
            # Accepted (or unresolved): settlement is reported later. Never resend.
            payout = await client.payouts.wait_for_settlement(payout.payout_id, timeout=60)
        else:
            payout = await client.payouts.get(payout.payout_id)
        print(payout.status, payout.is_terminal, payout.last_note)


asyncio.run(main())
```

`payouts.wait(payout_id, status=...)` / `until=lambda p: ...` is the general form (polling starts at 1 s, grows ×1.5
up to 5 s with jitter). On expiry it raises `WhireTimeoutError` with `.last` (the last `Payout` read) and `.elapsed`.
`wait_for_kyc` polls until the payout leaves `pending_kyc`; `wait_for_settlement` until it leaves `processing`. Call
the latter after `execute()` returned `processing`; after `confirm()` alone nothing settles.

### Execute outcomes and the "do not resend" rule

`payouts.execute()` returns a `PayoutExecution` or raises `PayoutExecutionRefused`:

| Outcome | What happened | What to do |
|---|---|---|
| `status == "paid"`, `provider_reference` set | settled | nothing; `payouts.get()` shows `paid` |
| `status == "processing"` | accepted, settlement pending | `wait_for_settlement()` |
| `status == "processing"`, `unresolved is True` | the rail did not confirm; the payment may or may not have gone out | **do not resend**; `wait_for_settlement()` — a retry is refused until the rail reports `paid` or `failed` |
| `PayoutExecutionRefused` (HTTP 400), `error_code == "execution_refused"` | refused before sending (status unchanged, history note added) **or** rejected by the rail (status now `failed`) | read `payouts.get()`; hand the decision to a human |
| `PayoutExecutionRefused`, `error_code == "already_executed"` | this payout was already sent | nothing; never create a replacement automatically |

A payout is executed at most once per record. A second unkeyed `execute` is refused (`already_executed`), and an
execute after an unresolved attempt is refused until the rail reports. The exception carries `needs_user_action=True`
and a `suggestion` that says so — an agent that receives its `to_agent_dict()` must stop and ask.

```python
import asyncio
import random
import string
import uuid

from whire import PayoutExecutionRefused, WhireClient


def random_iban() -> str:
    bban = "ABNA" + "".join(random.choices(string.digits, k=10))
    check = 98 - int("".join(str(int(c, 36)) for c in bban + "NL00")) % 97
    return f"NL{check:02d}{bban}"


async def main() -> None:
    run = uuid.uuid4().hex[:8]
    async with WhireClient(environment="sandbox") as client:
        caps = await client.capabilities()
        # 20.01 is only an "insufficient funds" trigger on the simulated rail; elsewhere it is an ordinary amount.
        amount = "20.01" if caps.simulated else "20.00"

        payee = await client.beneficiaries.create(
            full_name="Acme Supplies BV", iban="DE89370400440532013000", reference=f"acme-{run}"
        )
        mandate = await client.mandates.create(
            beneficiary_id=payee.beneficiary_id,
            debtor_name=f"Merchant {run} B.V.",
            debtor_iban=random_iban(),
            mandate_reference=f"REF-{run}",
            signed_by="Finance",
            max_amount=100,
        )
        payout = await client.payouts.create(
            beneficiary_id=payee.beneficiary_id, mandate_id=mandate.mandate_id, amount=amount
        )
        await client.payouts.submit(payout.payout_id)
        await client.payouts.wait_for_kyc(payout.payout_id, timeout=30)

        try:
            execution = await client.payouts.execute(payout.payout_id)
            print("sent:", execution.status, execution.provider_reference)
        except PayoutExecutionRefused as e:
            payout = await client.payouts.get(payout.payout_id)  # failed, or unchanged with a history note
            print("refused:", e.error_code, "|", e, "| status now:", payout.status)
            print(e.suggestion)  # Do not resend or create a replacement; ... hand the decision to a human.


asyncio.run(main())
```

## Idempotency

The service honours `Idempotency-Key` on every POST under `/api` (creates, activate, submit, confirm, execute, …) and
on `/x402/verify` and `/x402/settle`. Same key and body → the stored answer is replayed (`.replayed` is `True`); same key
while the first call is still running → 409 (`IdempotencyConflictError`, retried by the SDK); same key with a different
body → 422 (`IdempotencyMismatchError`). Keys live 24 hours. `POST /mcp` ignores the header.

With `auto_idempotency=True` (the default) the SDK sends a fresh UUID on every POST, and the same key and the same body
bytes on every retry of that call, so a retried create never makes two records. Every REST POST method also takes
`idempotency_key=` so you can pin one yourself, and every `WhireError` carries the key that was sent as
`e.idempotency_key`: if a call fails on the network after it may have reached the server, repeat it with that key to
get the stored outcome instead of a duplicate.

```python
import asyncio
import random
import string
import uuid

from whire import AmbiguousResponseError, NetworkError, WhireClient


def random_iban() -> str:
    bban = "ABNA" + "".join(random.choices(string.digits, k=10))
    check = 98 - int("".join(str(int(c, 36)) for c in bban + "NL00")) % 97
    return f"NL{check:02d}{bban}"


async def main() -> None:
    run = uuid.uuid4().hex[:8]
    async with WhireClient(environment="sandbox") as client:
        key = str(uuid.uuid4())
        details = dict(
            legal_name=f"Merchant {run} B.V.",
            contact_first_name="Eva",
            contact_last_name="Jansen",
            email=f"eva.{run}@example.com",
            phone="+31612345678",
            funding_sources=[{"type": "sepa", "destination": random_iban()}],
        )
        first = await client.payers.create(**details, idempotency_key=key)
        again = await client.payers.create(**details, idempotency_key=key)  # replayed, no second payer
        print(first.payer_id == again.payer_id, first.replayed, again.replayed)  # True False True

        # The pattern for money-moving calls: reuse the key from the exception, never resend blindly.
        payee = await client.beneficiaries.create(
            full_name="Acme Supplies BV", iban="DE89370400440532013000", reference=f"acme-{run}"
        )
        mandate = await client.mandates.create(
            beneficiary_id=payee.beneficiary_id,
            debtor_name=f"Merchant {run} B.V.",
            debtor_iban=random_iban(),
            mandate_reference=f"KEY-{run}",
            signed_by="Finance",
            max_amount=100,
        )
        payout = await client.payouts.create(
            beneficiary_id=payee.beneficiary_id, mandate_id=mandate.mandate_id, amount=20
        )
        await client.payouts.submit(payout.payout_id)
        await client.payouts.wait_for_kyc(payout.payout_id, timeout=30)

        # Pin the key yourself for money-moving calls and store it next to the payout id.
        execute_key = str(uuid.uuid4())
        try:
            execution = await client.payouts.execute(payout.payout_id, idempotency_key=execute_key)
        except AmbiguousResponseError:
            # The request may have been processed: read the record before doing anything else.
            raise
        except NetworkError as e:
            # The request never reached the server (already retried): repeating it with the same key is safe.
            execution = await client.payouts.execute(payout.payout_id, idempotency_key=e.idempotency_key)
        print(execution.status, execution.replayed)  # paid False

        # The same key later replays the stored answer instead of paying twice.
        replay = await client.payouts.execute(payout.payout_id, idempotency_key=execute_key)
        print(replay.replayed, replay.provider_reference == execution.provider_reference)  # True True


asyncio.run(main())
```

Retry policy in one paragraph: a request is *idempotent* when it is a GET, a POST carrying an `Idempotency-Key`,
`POST /x402/settle` (idempotent per signed payload), or a read-only MCP call. Idempotent requests are retried on
transport errors, 429, 409 and 5xx with full-jitter backoff (`retry_base_delay`, `retry_max_delay`; `Retry-After` is
honoured up to `retry_max_delay`). Non-idempotent requests (an unkeyed POST with `auto_idempotency=False`, mutating MCP
tools) are retried only when the request never reached the server (connect errors, 429, 503); a read timeout or a
500/502/504 raises `AmbiguousResponseError` at once. 400/401/404/422 are never retried. The worst case is
`(max_retries + 1) × read timeout + the sum of the delays`.

## Errors

Every exception derives from `WhireError`; `str(e)` is the server's sentence, and `e.to_agent_dict()` is a
JSON-serializable dict for agents (`error`, `error_code`, `status_code`, `retryable`, `needs_user_action`,
`is_input_error`, `suggestion`, `request_id`, `idempotency_key`). Exceptions never hold the httpx request or response,
and never contain the API key.

| Class | HTTP | `error_code` | retryable | needs_user_action | is_input_error |
|---|---|---|---|---|---|
| `AuthenticationError` | 401 | `auth_failed` | no | yes | no |
| `NotFoundError` | 400 `"<Kind> <id> not found."` | `not_found` | no | no | yes |
| `NotFoundError` | 404 (unknown route) | `route_not_found` | no | no | no |
| `BadRequestError` | 400 (also `ok:false` on a 2xx) | `bad_request` | no | no | yes |
| `PayoutExecutionRefused` (a `BadRequestError`) | 400 from `payouts.execute`; `None` from `x402.pay` (MCP-backed) | `execution_refused`, `already_executed` | no | yes | no |
| `IdempotencyConflictError` | 409 | `idempotency_in_flight` | yes | no | no |
| `IdempotencyMismatchError` | 422 | `idempotency_mismatch` | no | no | yes |
| `RateLimitError` (`.retry_after`) | 429 | `rate_limited` | yes | no | no |
| `ServerError` | 5xx | `server_error` | yes | no | no |
| `NetworkError` | — | `network_error` | yes | no | no |
| `AmbiguousResponseError` (a `NetworkError`) | — | `ambiguous_outcome` | no | yes | no |
| `ResponseFormatError` (`.payload`) | 2xx with an unexpected body | `invalid_response` | no | no | no |
| `InvalidInputError` | — (nothing was sent) | `invalid_input` | no | no | yes |
| `ToolError` (`.tool_name`, `.data`) | MCP `isError` | `unknown_tool`, `invalid_arguments`, `tool_error` | no | no | yes |
| `MCPProtocolError` (`.code`, `.data`) | JSON-RPC error | `mcp_error`, `method_not_found` | no | no | for -32602 / not found |
| `WhireTimeoutError` (`.payout_id`, `.last`, `.elapsed`) | — | `timeout` | no | no | no |

Two service conventions worth knowing: an unknown **record** is a 400 with the sentence `"Payout <id> not found."`
(mapped to `NotFoundError`, `not_found`), while a 404 means the **route** does not exist on that deployment — almost
always a wrong `base_url` or an SDK/server version mismatch. `ToolError` and `MCPProtocolError` escape only from
direct `client.mcp.*` calls; the namespace methods backed by MCP translate them into the REST-style classes, with
`status_code=None` (no HTTP status applies).

```python
import asyncio
import json

from whire import InvalidInputError, NotFoundError, WhireClient, WhireError


async def main() -> None:
    async with WhireClient(environment="sandbox") as client:
        try:
            await client.payouts.get("nope")
        except NotFoundError as e:
            print(e.status_code, e.error_code, e)  # 400 not_found Payout nope not found.
            print(json.dumps(e.to_agent_dict(), indent=2))

        try:
            await client.payouts.create(beneficiary_id="b", amount="1.234")  # rejected locally
        except InvalidInputError as e:
            print(e.error_code, e)

        try:
            await client.payouts.list(status="PAID")  # statuses are exact lower-case values
        except WhireError as e:
            print(type(e).__name__, e.is_input_error)


asyncio.run(main())
```

## Environments, self-hosting and authentication

| Deployment | How to reach it |
|---|---|
| Sandbox `https://sandbox.whire.ai` | `WhireClient(environment="sandbox")` — the default |
| Production `https://api.whire.ai` | `WhireClient(api_key=..., environment="production")` — a key is required |
| Your own deployment | `WhireClient(base_url="https://payouts.example.com", api_key=...)` |

Resolution order: `base_url=` > `environment=` > `WHIRE_BASE_URL` > `WHIRE_ENVIRONMENT` (`sandbox` / `production`) >
sandbox. The key comes from `api_key=` or `WHIRE_API_KEY` and is sent as `X-API-Key` (or `Authorization: Bearer` with
`auth_scheme="bearer"`). `/api/health`, `/api/capabilities` and `/x402/supported` need no key.

- The shared sandbox currently runs with authentication **off**: no key is needed anywhere, and the SDK logs one
  warning on the first unauthenticated request. Do not rely on that: the sandbox will turn production.
- A client for `api.whire.ai` without a key raises `AuthenticationError` in the constructor unless
  `allow_unauthenticated=True`.
- `base_url` must be `https://` unless the host is localhost, a loopback or an RFC 1918 address; pass
  `allow_insecure_http=True` to override. `verify=` takes `True` or a CA bundle path.
- One `httpx.AsyncClient` is created in `__init__` and shared by every namespace and `client.mcp`. It is safe for
  concurrent use from tasks on one event loop; do not share a client across loops or threads. Use `async with` or
  `await client.close()`; a request after `close()` raises `WhireError("client is closed")`.
- Start every integration with `capabilities()`: `settlement` says whether this deployment moves money at all and
  `simulated` whether it does so against simulated money.

```python
import asyncio

from whire import AuthenticationError, WhireClient


async def main() -> None:
    async with WhireClient(environment="sandbox") as client:
        print(client)  # WhireClient(base_url='https://sandbox.whire.ai', api_key=None)
        caps = await client.capabilities()
        print(caps.environment, caps.authorization, caps.settlement, caps.simulated)
        health = await client.health()
        print(health.status, health.payment_provider.rail if health.payment_provider else None)

    try:
        WhireClient(environment="production")  # raises unless WHIRE_API_KEY is set
    except AuthenticationError as e:
        print(e)  # api_key is required for production; set WHIRE_API_KEY


asyncio.run(main())
```

Other constructor options: `timeout` (read timeout; a float becomes `httpx.Timeout(connect=5, read=timeout, write=10,
pool=5)`), `execute_timeout=120` (read timeout for execute, settle and x402 pay), `max_retries=3`, `auto_idempotency`,
`user_agent`, and `transport=` for injecting an `httpx.MockTransport` in tests.

## Simulation triggers

On a deployment that runs the simulated rail, the cents of the amount and two designated IBANs select the outcome.
**They mean nothing anywhere else**: check `capabilities().simulated` first (the toolkit's `hint()` only mentions them
when a cached capabilities read said `simulated: true`). `simulation.get()` returns this table, the KYC/settlement delay
(`delay_ms`) and every simulated account with its balance.

| Trigger | Outcome | Status after execute → after the delay |
|---|---|---|
| any other amount | settled at once | `paid` |
| ends in `.01` | insufficient funds (AM04) | 400 refused; `failed` |
| ends in `.02` | payee account closed (AC04) | 400 refused; `failed` |
| ends in `.03` | no confirmation, then settled | `processing` (`unresolved`) → `paid` |
| ends in `.04` | no confirmation, then rejected | `processing` (`unresolved`) → `failed` |
| ends in `.05` | accepted, settles later | `processing` → `paid` |
| ends in `.06` | settled, then returned by the bank (AC01) | `paid` → `returned` |
| ends in `.07` | rail unavailable, nothing sent | 400 refused; `failed` |
| debtor IBAN `NL56SIML0000000001` | source account frozen, refused before sending | 400 refused; stays `approved` |
| payee IBAN `NL29SIML0000000002` | KYC rejected after the delay | `kyc_rejected` (terminal) |
| any other payee | KYC approved after the delay | `approved` |

Every simulated debtor account opens with 1000.00 EUR, so a balance below the amount is also insufficient funds. A
`returned` payout still counts against the mandate's total; `failed` and `kyc_rejected` do not.

```python
import asyncio

from whire import WhireClient


async def main() -> None:
    async with WhireClient(environment="sandbox") as client:
        caps = await client.capabilities()
        if not caps.simulated:
            print("no simulated rail here: amounts are just amounts")
            return
        sim = await client.simulation.get()
        print(sim.rail, sim.delay_ms, sim.triggers.frozen_source_iban if sim.triggers else None)
        for scenario in sim.scenarios:
            print(f"{scenario.trigger:35} -> {scenario.payout_status}", scenario.then or "")


asyncio.run(main())
```

`simulation.reset()` empties the **shared** store and ledger. The SDK refuses it unless `capabilities()` reports a
simulated `SANDBOX` deployment and never against `api.whire.ai` (`force=True` overrides); it is not exposed to agents.

## Agent toolkit

`WhireToolkit` serves the 33 server tools (same names, camelCase arguments and results as the hosted MCP server) to
any function-calling model, executes the calls through `WhireClient`, and turns every `WhireError` into the error dict
above instead of raising. Money-moving tools (`execute_payout`, `pay_x402_resource`) are gated: without
`allow_destructive=True` or a `confirm` callback they answer `confirmation_required`.

```python
import asyncio
import json

from whire import WhireToolkit


async def main() -> None:
    async with WhireToolkit(environment="sandbox") as toolkit:
        print(len(toolkit.tool_names), sorted(toolkit.destructive_tools))  # 33 ['execute_payout', 'pay_x402_resource']

        openai_tools = toolkit.get_tools("openai")  # Chat Completions: {"type": "function", "function": {...}}
        responses_tools = toolkit.get_tools("openai-responses")  # Responses API: {"type": "function", "name": ...}
        anthropic_tools = toolkit.get_tools("anthropic")  # {"name", "description", "input_schema"}
        mcp_tools = toolkit.get_tools("mcp")  # verbatim server definitions incl. outputSchema and annotations
        print(openai_tools[0]["function"]["name"], responses_tools[0]["name"], anthropic_tools[0]["name"], mcp_tools[0]["name"])

        # Narrow the set: only these tools, none of the destructive ones.
        read_only = toolkit.get_tools("anthropic", names={"get_capabilities", "list_payouts"}, exclude_destructive=True)
        print([t["name"] for t in read_only])

        # Execute a call the model made. Arguments are camelCase; snake_case keys and numeric strings are normalised.
        result = await toolkit.execute("get_capabilities", {})
        print(json.dumps(result))  # the verbatim server payload, e.g. {"authorization": true, "settlement": true, ...}

        # Guidance for the model, derived from the result. Append it to the tool result you send back.
        print(toolkit.hint("get_capabilities", {}, result))

        # Errors never raise; they come back as the error dict.
        error = await toolkit.execute("get_payout_status", {"payoutId": "nope"})
        print(error["error_code"], error["is_input_error"], error["suggestion"])

        # A destructive tool without a confirm callback is not executed.
        gated = await toolkit.execute("execute_payout", {"payoutId": "nope"})
        print(gated["error_code"], gated["needs_user_action"])  # confirmation_required True

        print(toolkit.system_prompt[:80], "...")


asyncio.run(main())
```

### Wiring the tool loop

The loop is the same for every provider: send `get_tools(<format>)`, run each tool call through `execute`, add the
`hint()` text, send the result back as a string.

```python
import asyncio
import json

from whire import WhireToolkit


async def tool_result(toolkit: WhireToolkit, name: str, arguments: dict) -> str:
    """What you send back to the model for one tool call."""
    result = await toolkit.execute(name, arguments)
    text = json.dumps(result)
    hint = toolkit.hint(name, arguments, result)
    return f"{text}\n\nHint: {hint}" if hint else text


async def main() -> None:
    async with WhireToolkit(environment="sandbox") as toolkit:
        # What a model would have produced:
        #   OpenAI Chat Completions: call.function.name, json.loads(call.function.arguments), call.id
        #   OpenAI Responses:        item.name, json.loads(item.arguments), item.call_id
        #   Anthropic Messages:      block.name, block.input (already a dict), block.id
        name, arguments = "list_payouts", {}
        print((await tool_result(toolkit, name, arguments))[:200])


asyncio.run(main())
```

Where the pieces go:

```text
OpenAI Chat Completions  tools=toolkit.get_tools("openai")            → {"role": "tool", "tool_call_id": call.id, "content": text}
OpenAI Responses         tools=toolkit.get_tools("openai-responses")  → {"type": "function_call_output", "call_id": item.call_id, "output": text}
Anthropic Messages       tools=toolkit.get_tools("anthropic")         → {"type": "tool_result", "tool_use_id": block.id, "content": text}
system prompt            toolkit.system_prompt
```

### Confirmation gating

`execute_payout` and `pay_x402_resource` move money. Give the toolkit a `confirm(name, arguments, summary)` callback
(sync or async) and it is called with a summary of what is about to happen — for `execute_payout` the payout's amount,
currency, status, beneficiary, counterparty IBAN and name, and mandate; for `pay_x402_resource` the quoted URL, amount,
asset, payee and mandate. Return `True` to proceed; anything else answers `confirmation_declined`. `allow_destructive=True`
skips the gate (use it only when the host prompts the user itself, as MCP clients do), and `require_confirmation=` (a
collection of tool names, never a plain string) adds tools to the gate. Without `allow_destructive=True` the two
money-moving tools stay gated even if `require_confirmation` omits them.

```python
import asyncio
import random
import string
import uuid

from whire import WhireClient, WhireToolkit


def random_iban() -> str:
    bban = "ABNA" + "".join(random.choices(string.digits, k=10))
    check = 98 - int("".join(str(int(c, 36)) for c in bban + "NL00")) % 97
    return f"NL{check:02d}{bban}"


def ask_human(name: str, arguments: dict, summary: dict) -> bool:
    print(f"{name}: send {summary.get('amount')} {summary.get('currency')} to {summary.get('counterpartyName')}?")
    return False  # a real integration asks the user; this one says no


async def main() -> None:
    run = uuid.uuid4().hex[:8]
    async with WhireClient(environment="sandbox") as client:
        payee = await client.beneficiaries.create(
            full_name="Acme Supplies BV", iban="DE89370400440532013000", reference=f"acme-{run}"
        )
        mandate = await client.mandates.create(
            beneficiary_id=payee.beneficiary_id,
            debtor_name=f"Merchant {run} B.V.",
            debtor_iban=random_iban(),
            mandate_reference=f"TK-{run}",
            signed_by="Finance",
            max_amount=100,
        )
        payout = await client.payouts.create(
            beneficiary_id=payee.beneficiary_id, mandate_id=mandate.mandate_id, amount=20
        )
        await client.payouts.submit(payout.payout_id)
        await client.payouts.wait_for_kyc(payout.payout_id, timeout=30)

        toolkit = WhireToolkit(client=client, confirm=ask_human)
        declined = await toolkit.execute("execute_payout", {"payoutId": payout.payout_id})
        print(declined["error_code"], declined["needs_user_action"])  # confirmation_declined True
        print((await client.payouts.get(payout.payout_id)).status)  # approved: nothing moved


asyncio.run(main())
```

## MCP

### Over HTTP (the hosted server)

The same 33 tools are served at `https://sandbox.whire.ai/mcp` (stateless streamable HTTP; plain JSON replies). Point
any MCP client at it:

```bash
claude mcp add --transport http whire https://sandbox.whire.ai/mcp --header "X-API-Key: $KEY"
```

`WhireMCPClient` talks JSON-RPC 2.0 to that endpoint without the `mcp` package; `client.mcp` is one that shares the
client's HTTP connection, key and retry policy. `ToolError` / `MCPProtocolError` surface only from these direct calls.

```python
import asyncio

from whire import ToolError, WhireClient


async def main() -> None:
    async with WhireClient(environment="sandbox") as client:
        info = await client.mcp.initialize()  # optional: the endpoint is stateless
        print(info.name, info.version, info.protocol_version)

        tools = await client.mcp.list_tools()
        print(len(tools), [t.name for t in tools if t.destructive])  # 33 ['execute_payout', 'pay_x402_resource']

        caps = await client.mcp.call_tool("get_capabilities", {})  # structuredContent as a dict
        print(caps["simulated"])

        provider = await client.mcp.read_resource("config://provider")
        print(provider.contents[0].media_type, provider.json)

        print([p.name for p in await client.mcp.list_prompts()])

        try:
            await client.mcp.call_tool("get_payout_status", {"payoutId": "nope"})
        except ToolError as e:
            print(e.error_code, e)  # tool_error Payout nope not found.


asyncio.run(main())
```

### The stdio server (Claude Desktop and other clients without a URL field)

`python -m whire.mcp_server` serves the same tools over stdio (newline-delimited JSON-RPC) and proxies resources and
prompts to the deployment's `/mcp`. Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "whire": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "whire.mcp_server"],
      "env": {
        "WHIRE_API_KEY": "your-api-key",
        "WHIRE_ENVIRONMENT": "sandbox"
      }
    }
  }
}
```

Use the absolute path of the interpreter you installed `whire` into (for a virtualenv, its `.venv/bin/python`): Claude
Desktop does not inherit your shell `PATH`, and on macOS there is often no `python` on it at all.

| Variable | Meaning |
|---|---|
| `WHIRE_API_KEY` | the key; required when `WHIRE_ENVIRONMENT=production` (the server exits otherwise) |
| `WHIRE_BASE_URL` | a self-hosted deployment (wins over `WHIRE_ENVIRONMENT`) |
| `WHIRE_ENVIRONMENT` | `sandbox` (default) or `production` |
| `WHIRE_ALLOW_DESTRUCTIVE` | default `true`: the MCP host prompts the user per tool call, so the server does not gate again |
| `WHIRE_TIMEOUT` | read timeout in seconds (default 30) |
| `WHIRE_MCP_HELPERS` | `1` adds the SDK-only `wait_for_payout` tool (absent from the hosted server) |
| `WHIRE_LOG_LEVEL` | stderr log level (`DEBUG`, `INFO` default, `WARNING`, ...); stdout carries only protocol messages |

## x402 over SEPA

```
agent  → GET /report                        seller → 402 + PAYMENT-REQUIRED
agent    checks the mandate, creates the payout, signs
agent  → GET /report + PAYMENT-SIGNATURE    seller → POST /x402/verify, POST /x402/settle
                                            seller → 200 + resource + PAYMENT-RESPONSE
```

### The facilitator side (a seller)

`x402.supported()` says what this deployment can verify and settle (empty on a deployment with no rail). `x402.verify()`
checks a decoded `PAYMENT-SIGNATURE` payload against the requirement you published — signature, expiry, payee/amount/
asset, payout state, mandate — and moves nothing. `x402.settle()` verifies again and executes the payout. Both answer
200 for everything and never raise on `is_valid: false` / `success: false`; read the fields. Settle is idempotent per
signed payload, so a seller that got no answer can settle again; while it is still pending, `result.pending` is `True`:
settle again later, never re-pay. The reference seller settles before serving (a bank transfer is not pre-funded).

```python
import asyncio

from whire import WhireClient

requirement = {
    "scheme": "sepa-mandate",
    "network": "sepa:eu",
    "amount": "1.00",
    "asset": "EUR",
    "payTo": "DE89370400440532013000",
    "maxTimeoutSeconds": 300,
    "extra": {"payeeName": "Report Vendor BV", "reference": "x402 Q3 market report"},
}
# The decoded PAYMENT-SIGNATURE header from the agent, passed through untouched (signed strings stay strings).
payment = {
    "x402Version": 2,
    "resource": {"url": "https://vendor.example/report", "description": "Q3 market report"},
    "accepted": requirement,
    "payload": {
        "payoutId": "b1f75ef4-c754-4a58-8d18-bdbf0042e453",
        "mandateReference": "X402-001",
        "authorization": {
            "from": "NL91ABNA0417164300",
            "to": "DE89370400440532013000",
            "value": "1.00",
            "asset": "EUR",
            "validBefore": "1790191414",
            "nonce": "4f2c9a1e7b3d",
        },
        "signature": "sepa-mandate-sig-not-really-signed",
    },
}


async def main() -> None:
    async with WhireClient(environment="sandbox") as client:
        supported = await client.x402.supported()
        print(supported.can_settle, [(k.scheme, k.network) for k in supported.kinds])

        verdict = await client.x402.verify(payment_payload=payment, payment_requirements=requirement)
        print(verdict.is_valid, verdict.invalid_reason)  # False <why>

        if verdict.is_valid:
            settled = await client.x402.settle(payment_payload=payment, payment_requirements=requirement)
            if settled.success:
                print("serve; PAYMENT-RESPONSE =", settled.raw)
            elif settled.pending:
                print("settle again later with the same payload:", settled.error_reason)
            else:
                print("refuse:", settled.error_reason)


asyncio.run(main())
```

### The payer side (an agent)

`x402.quote(url=...)` reads a 402 and commits nothing. `x402.pay(url=..., mandate_id=...)` checks the mandate for the
quoted amount and payee, creates the payout, waits for KYC, signs, and retries the request; refused by the mandate →
nothing is created. It is destructive and never retried by the SDK; a business refusal raises `PayoutExecutionRefused`.
The two payer tools exist only on deployments that can settle.

```python
import asyncio

from whire import WhireClient


async def main() -> None:
    async with WhireClient(environment="sandbox") as client:
        quote = await client.x402.quote(url="https://sandbox.whire.ai/api/health")
        print(quote.status, quote.free)  # 200 True: nothing to pay
        if not quote.free and quote.requirement is not None:
            print("price:", quote.requirement.amount, quote.requirement.asset, "to", quote.requirement.pay_to)
            paid = await client.x402.pay(url=quote.url, mandate_id="<mandate id>", reason="Q3 report")
            print(paid.paid, paid.status, paid.payout_id)


asyncio.run(main())
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `WhireError` with `error_code="invalid_response"`, status 403, message `error code: 1010` | the Cloudflare edge in front of the sandbox blocks some User-Agents (e.g. `Python-urllib/3.13`) | keep the SDK's default `User-Agent` (`whire-python/<v> httpx/<v>`); if you set `user_agent=`, use a browser-like or product name |
| `MCPProtocolError` after a 406 on `/mcp` | the request lacked `Accept: application/json, text/event-stream` | the SDK always sends it; other clients must too |
| `NotFoundError` with `error_code="route_not_found"` (404) | `base_url` points at something that is not the agent-payouts API, or the deployment is older than the SDK | check `base_url` (no path suffix) and `capabilities()` |
| `NotFoundError` with `error_code="not_found"` (400 `"... not found."`) | unknown record id | list the records; the store is shared and a redeploy or reset wipes it |
| `AuthenticationError` at construction | production without a key | pass `api_key=` or set `WHIRE_API_KEY` |
| `IdempotencyMismatchError` (422) | a key reused with a different body — even `20.1` vs `20.10` or a different key order | generate a new key for a new request |
| `BadRequestError` on a payer create | the IBAN is already a funding source of another payer, or the email is registered | use per-run unique data (see [RUNNING_AGAINST_A_DEPLOYMENT.md](RUNNING_AGAINST_A_DEPLOYMENT.md)) |
| `WhireTimeoutError` from `wait_for_kyc` | the deployment has no rail, so nobody records KYC | record it yourself with `payouts.record_event(id, event="kyc_approved")` |
| `InvalidInputError` from `payouts.list(status="PAID")` | statuses are exact lower-case values; the SDK rejects unknown ones locally | use `PayoutStatus` members |
| `payouts.list(status=...)` returns `[]` | no payout is in that status on this deployment (the store is shared and wiped by a redeploy or reset) | check `payouts.list()` and the ids you created |

## Non-goals

- A sync client: the SDK is async only (`asyncio.run` around it is the sync story).
- Webhooks: the service has none; poll with `payouts.wait*`.
- Client-side rate limiting: the service does not rate-limit today; 429s are retried with backoff.
- A generic x402 seller middleware: the facilitator calls are provided, the HTTP framework glue is yours.

## Development

`RUNNING_AGAINST_A_DEPLOYMENT.md` explains the shared sandbox store, the uniqueness rules, the reset guard and how to
run the opt-in live tests (`WHIRE_LIVE=1`). Unit tests need no network:

```bash
pip install -e ".[dev]"
pytest -q
```

## License

MIT
