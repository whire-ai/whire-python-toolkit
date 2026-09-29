# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

## [0.2.0] - 2026-09-25

A rewrite. Version 0.1.0 wrapped an API (`/recipients`, `/payments/send`, `/payments/balance`, `/payments/mandates`,
`/payments/debit`) that the Whire service does not serve; every one of those routes answers 404 on
`https://sandbox.whire.ai`. 0.2.0 targets the real **agent-payouts** API: payers, users, beneficiaries, signed
mandates with ten checks, signed authorization receipts, the payout lifecycle over a (simulated) SEPA rail, x402 over
SEPA, and the MCP server at `/mcp`. Nothing from 0.1.0 is kept for compatibility because none of it could work.

### Breaking

- `WhireClient` is now namespaced: `client.payers`, `client.users`, `client.beneficiaries`, `client.mandates`,
  `client.authorizations`, `client.payouts`, `client.simulation`, `client.x402`, plus `client.health()`,
  `client.capabilities()` and `client.mcp` (a `WhireMCPClient`). All method parameters are keyword-only except the
  leading resource id.
- `custom_base_url=` is now `base_url=`; `http://` is accepted only for localhost / loopback / RFC 1918 hosts unless
  `allow_insecure_http=True`. A production client (`api.whire.ai`) without a key raises `AuthenticationError` at
  construction unless `allow_unauthenticated=True`.
- Responses follow the service's camelCase wire format; models expose snake_case attributes with camelCase aliases,
  `Decimal` money and aware UTC datetimes. Every model carries `.raw` (the verbatim wire object) and `.replayed`.
- The error hierarchy was rebuilt (`NotFoundError`, `BadRequestError`, `PayoutExecutionRefused`,
  `IdempotencyConflictError`, `IdempotencyMismatchError`, `RateLimitError`, `ServerError`, `NetworkError`,
  `AmbiguousResponseError`, `ResponseFormatError`, `InvalidInputError`, `ToolError`, `MCPProtocolError`,
  `WhireTimeoutError`, all under `WhireError`). `to_agent_dict()` now also carries `status_code`, `request_id`
  and `idempotency_key` (alongside `error`, `error_code`, `retryable`, `needs_user_action`, `is_input_error`,
  `suggestion`).
- `WhireToolkit` exposes the 33 server tools (same names, camelCase arguments and results as the MCP server) instead
  of the old `create_recipient` / `send_payment` set. `get_tools()` takes a `format` (`"openai"`, `"openai-responses"`,
  `"anthropic"`, `"mcp"`); money-moving tools (`execute_payout`, `pay_x402_resource`) are gated behind
  `allow_destructive=True` or a `confirm` callback.
- The stdio MCP server (`python -m whire.mcp_server`) speaks newline-delimited JSON-RPC (the framing Claude Desktop
  expects) instead of `Content-Length` headers.

### Added

- Full REST coverage: payers (create, activate, suspend, funding sources), beneficiaries and IBAN validation,
  mandates (create, validate, revoke), signed authorization receipts (create, byte-exact verify), payouts (create,
  submit, events, confirm, execute), simulation, health and capabilities.
- MCP-only operations exposed as ordinary methods: users (`register_user`, payment methods,
  `create_beneficiary_for_user`), `beneficiaries.get`, `payers.set_default_funding_source`, `x402.quote`, `x402.pay`.
- x402 facilitator side: `x402.supported()`, `x402.verify()`, `x402.settle()` with `X402SettleResult.pending`.
- Polling helpers `payouts.wait()`, `wait_for_kyc()`, `wait_for_settlement()` with growing intervals, jitter and
  `WhireTimeoutError(.last)`.
- Automatic `Idempotency-Key` on every POST (`auto_idempotency=True`), replay detection (`.replayed`), and
  `idempotency_key` on every REST POST method so a network-failed call can be repeated safely.
- A retry policy that distinguishes idempotent requests (retried on 429/409/5xx and transport errors) from
  non-idempotent ones (retried only when the request never reached the server; otherwise `AmbiguousResponseError`).
- `WhireMCPClient`: JSON-RPC 2.0 over HTTP to `/mcp` with no `mcp` package dependency (tools, resources, prompts,
  completion, ping, raw `request`).
- `WhireToolkit.hint()` for agent guidance derived from tool results, and a rewritten `SYSTEM_PROMPT`.
- Environment variables `WHIRE_API_KEY`, `WHIRE_BASE_URL`, `WHIRE_ENVIRONMENT`.
- `pytest` marker `live` and an opt-in end-to-end suite (`WHIRE_LIVE=1`) that never resets the shared sandbox.
- `RUNNING_AGAINST_A_DEPLOYMENT.md`.

### Removed

- `whire.mandate` (client-side mandate objects), `create_recipient`, `list_recipients`, `pay`, `get_balance`,
  `get_transactions`, `create_mandate` / `debit` (direct debit), the `consent_url` flow, the old tool set and prompts,
  `TESTING_LOCALLY.md`, the `whire_test_key` public key (the sandbox runs with authentication off today).

### Fixed

- Every request now reaches an endpoint that exists.
- Secrets hygiene: the API key lives only on the transport, never appears in `repr`, `str(e)`, `to_agent_dict()` or
  logs; exceptions no longer hold the httpx request or response.
- A recognisable `User-Agent` (`whire-python/<version> httpx/<version>`) so the Cloudflare edge in front of the
  sandbox does not answer 403.

