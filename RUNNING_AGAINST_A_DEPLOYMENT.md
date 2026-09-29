# Running against a deployment

How to point the SDK, the toolkit, the stdio MCP server and the test suites at a real agent-payouts deployment, and
what to keep in mind when that deployment is the shared public sandbox.

## Base URL

| Deployment | `base_url` | How |
|---|---|---|
| Public sandbox | `https://sandbox.whire.ai` | `WhireClient(environment="sandbox")` (the default) |
| Production | `https://api.whire.ai` | `WhireClient(api_key=..., environment="production")` |
| Self-hosted | anything else | `WhireClient(base_url="https://payouts.example.com", api_key=...)` |

Resolution order: `base_url=` > `environment=` > `WHIRE_BASE_URL` > `WHIRE_ENVIRONMENT` > sandbox. The base URL is
the origin only: the SDK appends `/api/...`, `/x402/...` and `/mcp` itself. A 404 (`NotFoundError` with
`error_code="route_not_found"`) means the URL does not serve this API (a path suffix, a different service, or a
deployment older than the SDK); an unknown record is a 400 with `"... not found."`.

`base_url` must be `https://` unless the host is `localhost`, a loopback or an RFC 1918 address (a local dev
server); `allow_insecure_http=True` overrides that. `verify=` takes `True` or the path to a CA bundle for deployments
with a private CA.

Start with `await client.capabilities()`:

- `authorization: true, settlement: false` — the deployment authorizes only; payouts cannot be executed and the two
  x402 payer tools are absent (the MCP list has 31 tools).
- `settlement: true, simulated: true` — the simulated rail is on: payouts execute against simulated money, KYC is
  decided by the service after `delayMs`, and the cent-value / SIML-IBAN triggers select outcomes.
- `settlement: true, simulated: false` — real money. Nothing in the trigger table applies.

The docs are served by every deployment at `/docs` (sandbox: <https://sandbox.whire.ai/docs>). `docs.whire.ai` does
not resolve.

## Authentication

The key is sent as `X-API-Key` (or `Authorization: Bearer` with `auth_scheme="bearer"`). Three paths need no key:
`/api/health`, `/api/capabilities`, `/x402/supported`.

The shared sandbox currently runs with authentication **off**: any key, or none, works. The SDK logs one warning on
the first unauthenticated request and otherwise behaves the same. Build your integration as if the key were required;
the sandbox is expected to turn production. A client for `api.whire.ai` without a key raises `AuthenticationError` in
the constructor unless `allow_unauthenticated=True`.

## The shared store and its uniqueness rules

State is in memory on the deployment, there is one key per deployment, and every caller sees every record. On the
public sandbox that means:

- Lists (`payers.list()`, `mandates.list()`, `payouts.list()`, `users.list()`, MCP `resources/list`) contain other
  people's records. Filter by the ids you created; never assert on counts.
- A redeploy or a reset wipes everything. Do not keep ids across days.
- A funding-source IBAN belongs to at most one payer, a payer email can be registered once, and the same IBAN cannot be
  added twice to a payer. Both answer 400 (`BadRequestError`). Generate a fresh, checksum-valid IBAN and a unique email
  per run — the README's `random_iban()` helper does the former — and never register the docs' sample IBANs
  (`NL91ABNA0417164300` and friends) as a payer's funding source.
- `POST /api/beneficiaries` creates a new record every time (no dedup), so the docs' payee IBAN
  `DE89370400440532013000` is fine for beneficiaries. MCP `create_beneficiary_for_user` reuses **any** existing
  beneficiary with that IBAN store-wide, possibly one created by someone else under another name.
- Mandate references are not required to be unique, but unique ones (`SHOP-<run id>`) make records easy to find.
- Every simulated debtor account opens with 1000.00 EUR; an IBAN shared by many runs drains. A fresh debtor IBAN per
  run avoids surprise `Insufficient funds (AM04)` outcomes.
- Idempotency keys live 24 hours store-wide: use UUIDs, never fixed strings.

## Reset

`POST /api/reset` empties the store and the simulated ledger for **everyone** using the deployment. Never call it on
the shared sandbox. `client.simulation.reset()` refuses unless `capabilities()` reports a simulated `SANDBOX`
deployment and always refuses against `api.whire.ai`; `force=True` skips the guard for a deployment you own. Reset is
not exposed through `WhireToolkit`, `TOOLS` or the stdio server, and the live tests never call it.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `WHIRE_API_KEY` | client, toolkit, stdio server | the key (blank = none) |
| `WHIRE_BASE_URL` | client, toolkit, stdio server | explicit deployment origin; wins over `WHIRE_ENVIRONMENT` |
| `WHIRE_ENVIRONMENT` | client, toolkit, stdio server | `sandbox` (default) or `production`; production without a key fails fast |
| `WHIRE_ALLOW_DESTRUCTIVE` | stdio server | default `true` (the MCP host prompts per tool call); `false` gates `execute_payout` / `pay_x402_resource` |
| `WHIRE_TIMEOUT` | stdio server | read timeout in seconds (default 30) and the drain bound at shutdown |
| `WHIRE_MCP_HELPERS` | stdio server | `1` serves the SDK-only `wait_for_payout` tool |
| `WHIRE_LOG_LEVEL` | stdio server | stderr log level (`DEBUG`, `INFO` default, `WARNING`, ...); stdout carries only protocol messages |
| `WHIRE_LIVE` | tests | `1` enables the live suite |

## Tests

Unit tests use `httpx.MockTransport` and need no network:

```bash
pip install -e ".[dev]"
pytest -q
```

The live suite (`tests/live/test_live_sandbox.py`, marker `live`) runs the whole flow against a deployment: health and
capabilities, payer → activate → beneficiary → mandate → validate → authorize → verify → payout → submit →
`wait_for_kyc` → execute → paid, the confirm path, the simulated-rail outcomes (`.01` refused and `failed`, `.03`
unresolved then paid), keyed execute replay, x402 quote, the users flow over MCP, and full parity between `TOOLS` and
the server's `tools/list`. It is skipped unless opted in:

```bash
WHIRE_LIVE=1 pytest -q tests/live
WHIRE_LIVE=1 WHIRE_BASE_URL=https://payouts.example.com WHIRE_API_KEY=... pytest -q tests/live
```

It creates per-run unique data (random checksum-valid IBANs, unique emails and references), never reuses the docs'
sample IBANs for payers, and never calls reset. Expect a run to take a minute: the simulated rail decides KYC and
settlement after `delayMs` (3 s on the sandbox). The simulated-outcome assertions are skipped when `capabilities()`
says `simulated: false`.

## Behaviour that differs between deployments

- KYC: with a rail, `submit` → `pending_kyc` resolves by itself; poll. Without one, record the decision yourself with
  `payouts.record_event(id, event="kyc_approved")`, else `wait_for_kyc` times out.
- Execute: on a deployment that cannot settle (`capabilities().settlement` is false) `payouts.execute()` raises
  `PayoutExecutionRefused` with `"Real payment execution is not available: No settlement rail is configured. ..."`;
  `"... must be approved (or confirmed into processing) first."` means the payout is in the wrong status, on any
  deployment.
- `GET /api/payouts?status=` is the only server-side filter; the SDK applies every other filter client-side.
- Timestamps are ISO-8601 UTC with milliseconds; the SDK sends `...Z` with milliseconds and accepts dates.
- Responses carry `x-railway-request-id` / `cf-ray`; the SDK exposes it as `e.request_id` — quote it when reporting a
  problem.
