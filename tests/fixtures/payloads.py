"""Canned wire payloads copied from https://sandbox.whire.ai/docs and live probes (2026-09-25).

Every dict is the decoded ``data`` object (or the plain body for x402 / MCP replies) exactly as the
server sends it, with millisecond timestamps. Use ``fresh()`` before mutating one in a test.
"""

from __future__ import annotations

import copy
import json
from typing import Any

# --------------------------------------------------------------------------- ids

PAYER_ID = "95df7fad-016c-4406-bd04-8ff13ac121ee"
SOURCE_ID = "70f228c6-a6d5-4057-b2b5-46c4f57525b2"
SOURCE_ID_2 = "948857c9-e456-491f-8ef4-596e7208d0a5"
BENEFICIARY_ID = "9596acc9-0320-4b89-8cc8-475f04fe3948"
MANDATE_ID = "fde3a3a0-8125-4ea1-b6fc-a3e1c7138ead"
PAYOUT_ID = "702cc3c2-071c-45a0-bc92-2c22b1608e04"
PAYOUT_ID_UNRESOLVED = "de2a4e0d-7484-4caf-9802-1915e5cc9417"
PAYOUT_ID_FAILED = "0675a91c-b3bc-4495-9f7a-61f3e924da73"
PAYOUT_ID_NO_MANDATE = "934dda3f-915f-4ac1-9c95-62ac759df5b7"
RECEIPT_ID = "auth_93c446d771524834b72915e906ed06b6"
USER_ID = "f9ab45fa-369d-4b3f-a98a-1f0b95d56723"
METHOD_ID = "ec3441bc-17ab-441b-bc0a-14f2902c5aa1"

PAYER_IBAN = "NL91ABNA0417164300"
PAYER_IBAN_2 = "DE89370400440532013000"
BENEFICIARY_IBAN = "DE89370400440532013000"
USER_IBAN = "NL27ABNA3847969704"
FROZEN_SOURCE_IBAN = "NL56SIML0000000001"
KYC_REJECTED_IBAN = "NL29SIML0000000002"

T0 = "2026-09-23T18:42:40"  # most docs timestamps share this second

# --------------------------------------------------------------------------- service

HEALTH: dict[str, Any] = {
    "status": "ok", "transport": "http", "payouts": 0, "mandates": 0,
    "paymentProvider": {"configured": True, "rail": "sepa-simulation", "environment": "SANDBOX", "canMoveMoney": True, "simulated": True},
}
CAPABILITIES: dict[str, Any] = {
    "authorization": True, "settlement": True, "simulated": True, "environment": "SANDBOX",
    "note": "This deployment authorizes payments and settles them in simulation. No money moves.",
}
CAPABILITIES_AUTH_ONLY: dict[str, Any] = {
    **CAPABILITIES, "settlement": False, "simulated": False, "note": "This deployment authorizes payments only. Settlement is performed elsewhere.",
}
CAPABILITIES_PRODUCTION: dict[str, Any] = {
    **CAPABILITIES, "simulated": False, "environment": "PRODUCTION", "note": "This deployment authorizes payments and settles them over SEPA.",
}

# --------------------------------------------------------------------------- payers

FUNDING_SOURCE: dict[str, Any] = {
    "type": "sepa", "label": "Bank account 4300", "identifier": PAYER_IBAN, "holderName": "Merchant B.V.",
    "isDefault": True, "verified": False, "sourceId": SOURCE_ID, "createdAt": f"{T0}.612Z",
}
FUNDING_SOURCE_2: dict[str, Any] = {
    **FUNDING_SOURCE, "label": "Bank account 3000", "identifier": PAYER_IBAN_2, "isDefault": False, "sourceId": SOURCE_ID_2, "createdAt": f"{T0}.615Z",
}
PAYER_PENDING: dict[str, Any] = {
    "payerId": PAYER_ID, "accountType": "business", "legalName": "Merchant B.V.", "registrationNumber": "87654321", "vatNumber": "NL123456789B01",
    "contactFirstName": "Eva", "contactLastName": "Jansen", "email": "finance@merchant.example", "phone": "+31612345678",
    "fundingSources": [FUNDING_SOURCE], "status": "pending_verification",
    "history": [{"status": "pending_verification", "timestamp": f"{T0}.612Z", "note": "Account created, awaiting verification."}],
    "createdAt": f"{T0}.612Z", "updatedAt": f"{T0}.612Z",
}
PAYER_ACTIVE: dict[str, Any] = {
    **PAYER_PENDING, "fundingSources": [FUNDING_SOURCE, FUNDING_SOURCE_2], "status": "active", "updatedAt": f"{T0}.616Z",
    "history": PAYER_PENDING["history"] + [{"status": "active", "timestamp": f"{T0}.616Z", "note": "KYB documents checked"}],
}
PAYER_SUSPENDED: dict[str, Any] = {**PAYER_ACTIVE, "status": "suspended", "updatedAt": f"{T0}.861Z"}
PAYERS_LIST: dict[str, Any] = {"payers": [PAYER_ACTIVE]}

# --------------------------------------------------------------------------- users (MCP only)

PAYMENT_METHOD: dict[str, Any] = {
    "type": "sepa", "destination": USER_IBAN, "holderName": "Smoke User", "label": "Bank account 9704",
    "isDefault": True, "verified": False, "methodId": METHOD_ID, "createdAt": "2026-09-25T11:40:31.700Z",
}
USER: dict[str, Any] = {
    "userId": USER_ID, "firstName": "Smoke", "lastName": "User", "email": "user@example.com", "phone": "+31698765432",
    "paymentMethods": [PAYMENT_METHOD], "createdAt": "2026-09-25T11:40:31.700Z", "updatedAt": "2026-09-25T11:40:31.700Z",
}
USERS_LIST: dict[str, Any] = {"users": [USER]}
BENEFICIARY_FOR_USER: dict[str, Any] = {
    "beneficiaryId": "ae05b201-7f06-4202-80a5-0b3aaa921998", "fullName": "Smoke User", "iban": USER_IBAN,
    "reference": f"user:{USER_ID}", "email": "user@example.com", "createdAt": "2026-09-25T11:40:32.056Z",
}

# --------------------------------------------------------------------------- beneficiaries

BENEFICIARY: dict[str, Any] = {
    "beneficiaryId": BENEFICIARY_ID, "fullName": "Acme Supplies BV", "iban": BENEFICIARY_IBAN,
    "reference": "acme", "email": "billing@acme.example", "createdAt": f"{T0}.619Z",
}
IBAN_VALIDATION: dict[str, Any] = {
    "originalIban": "DE89 3704 0044 0532 0130 00", "normalizedIban": "DE89370400440532013000", "isValid": True,
    "countryCode": "DE", "formatValid": True, "checksumValid": True, "explanation": "IBAN is valid.",
}
IBAN_VALIDATION_INVALID: dict[str, Any] = {
    **IBAN_VALIDATION, "originalIban": "NL91ABNA0417164301", "normalizedIban": "NL91ABNA0417164301", "isValid": False,
    "countryCode": "NL", "checksumValid": False, "explanation": "IBAN checksum is invalid.",
}

# --------------------------------------------------------------------------- mandates

MANDATE: dict[str, Any] = {
    "mandateId": MANDATE_ID, "mandateReference": "SHOP-001", "beneficiaryId": BENEFICIARY_ID, "rail": "sepa", "payerId": PAYER_ID,
    "debtorName": "Merchant B.V.", "debtorIban": PAYER_IBAN, "scheme": "agent_payout", "mandateType": "recurring", "currency": "EUR",
    "maxAmount": 100, "maxTotalAmount": 250, "validFrom": f"{T0}.621Z", "validUntil": f"2027-09-23T18:42:40.621Z",
    "signedBy": "Finance", "signedAt": f"{T0}.622Z", "signature": "mandate-sig-527a620bb6625e3bc2e9421163ff2714", "status": "active",
    "createdAt": f"{T0}.622Z", "updatedAt": f"{T0}.622Z",
    "history": [{"status": "active", "timestamp": f"{T0}.622Z", "note": "Mandate SHOP-001 signed by Finance."}],
}
MANDATE_NO_PAYER: dict[str, Any] = {
    key: value for key, value in {**MANDATE, "mandateId": "2b1e0a4c-6f5d-4d7e-9a1b-3c4d5e6f7a8b"}.items() if key not in ("payerId", "maxTotalAmount")
}
MANDATE_REVOKED: dict[str, Any] = {
    **MANDATE, "status": "revoked", "updatedAt": f"{T0}.861Z",
    "history": MANDATE["history"] + [{"status": "revoked", "timestamp": f"{T0}.861Z", "note": "Withdrawn by finance"}],
}
MANDATES_LIST: dict[str, Any] = {"mandates": [MANDATE]}

CHECKS_PASSED: list[dict[str, Any]] = [
    {"name": "mandate_reference_format", "passed": True, "detail": "Mandate reference SHOP-001 matches the mandate reference rules."},
    {"name": "debtor_source_valid", "passed": True, "detail": "Debtor IBAN NL91ABNA0417164300 is valid."},
    {"name": "signature_intact", "passed": True, "detail": "Mandate signature matches the signed mandate fields."},
    {"name": "mandate_active", "passed": True, "detail": "Mandate status is active."},
    {"name": "validity_window", "passed": True, "detail": "Mandate is inside its validity window (2026-09-23T18:42:40.621Z to 2027-09-23T18:42:40.621Z)."},
    {"name": "mandate_type_usage", "passed": True, "detail": "Recurring mandate has authorized 0 payout(s) so far."},
    {"name": "currency_match", "passed": True, "detail": "Payment currency EUR matches the mandate currency."},
    {"name": "beneficiary_match", "passed": True, "detail": "Payment beneficiary matches the mandate beneficiary."},
    {"name": "per_payment_limit", "passed": True, "detail": "Payment amount 50 EUR is within the per-payment limit of 100 EUR."},
    {"name": "cumulative_limit", "passed": True, "detail": "Mandate total after this payment would be 50 of 250 EUR."},
]
CHECKS_REFUSED: list[dict[str, Any]] = CHECKS_PASSED[:8] + [
    {"name": "per_payment_limit", "passed": False, "detail": "Payment amount 500 EUR exceeds the per-payment limit of 100 EUR."},
    {"name": "cumulative_limit", "passed": False, "detail": "Mandate total after this payment would be 500, above the cumulative limit of 250 EUR."},
]
REFUSED_EXPLANATION = (
    "Payment amount 500 EUR exceeds the per-payment limit of 100 EUR. Mandate total after this payment would be 500, above the cumulative limit of 250 EUR."
)
MANDATE_VALIDATION: dict[str, Any] = {
    "mandateId": MANDATE_ID, "mandateReference": "SHOP-001", "status": "active", "scheme": "agent_payout", "mandateType": "recurring",
    "isValid": True, "checks": CHECKS_PASSED, "failedChecks": [], "usage": {"payoutCount": 0, "totalAmount": 0}, "remainingAmount": 250,
    "evaluatedContext": {"amount": 50, "currency": "EUR", "beneficiaryId": BENEFICIARY_ID}, "evaluatedAt": f"{T0}.625Z",
    "explanation": "Mandate SHOP-001 authorizes this payment.",
}
MANDATE_VALIDATION_REFUSED: dict[str, Any] = {
    **MANDATE_VALIDATION, "isValid": False, "checks": CHECKS_REFUSED, "failedChecks": ["per_payment_limit", "cumulative_limit"],
    "evaluatedContext": {"amount": 500, "currency": "EUR", "beneficiaryId": BENEFICIARY_ID}, "explanation": REFUSED_EXPLANATION,
}

# --------------------------------------------------------------------------- authorization

RECEIPT: dict[str, Any] = {
    "receiptId": RECEIPT_ID, "decision": "approved", "mandateId": MANDATE_ID, "mandateReference": "SHOP-001", "payer": PAYER_IBAN,
    "payee": BENEFICIARY_IBAN, "payeeName": "Acme Supplies BV", "amount": "50.00", "currency": "EUR", "checks": CHECKS_PASSED,
    "failedChecks": [], "explanation": "Mandate SHOP-001 authorizes this payment.", "remainingAmount": 250,
    "issuedAt": f"{T0}.626Z", "expiresAt": "2026-09-23T18:47:40.626Z", "fundsReserved": False,
    "signature": "auth-sig-e7647acd85cda9a233f73d96271278a72cd0a024d58cb598336e306fbe92f74a",
}
RECEIPT_REFUSED: dict[str, Any] = {
    **RECEIPT, "receiptId": "auth_5f16ca3670e74f4f8f4cb67d9bf465e8", "decision": "refused", "amount": "500.00", "checks": CHECKS_REFUSED,
    "failedChecks": ["per_payment_limit", "cumulative_limit"], "explanation": REFUSED_EXPLANATION,
    "issuedAt": f"{T0}.627Z", "expiresAt": "2026-09-23T18:47:40.627Z",
    "signature": "auth-sig-85dcbfcf5fd3b4b7e4c56566445700915e5fa80a386b7a13a5804d7e7b8ccf94",
}
RECEIPT_VERIFICATION: dict[str, Any] = {
    "signatureValid": True, "expired": False, "decision": "approved", "usable": True, "explanation": "Receipt is valid, unexpired, and records an approval.",
}
RECEIPT_VERIFICATION_TAMPERED: dict[str, Any] = {
    **RECEIPT_VERIFICATION, "signatureValid": False, "usable": False,
    "explanation": "Receipt signature does not verify; it was not issued by this service, or it was altered.",
}

# --------------------------------------------------------------------------- payouts


def _entry(status: str, timestamp: str, note: str) -> dict[str, Any]:
    return {"status": status, "timestamp": timestamp, "note": note}


PAYOUT_DRAFT: dict[str, Any] = {
    "payoutId": PAYOUT_ID, "beneficiaryId": BENEFICIARY_ID, "mandateId": MANDATE_ID, "amount": 50, "currency": "EUR",
    "reason": "Invoice 2026-091", "provider": "outsourced-provider", "status": "draft", "createdAt": f"{T0}.628Z", "updatedAt": f"{T0}.628Z",
    "history": [_entry("draft", f"{T0}.628Z", "Draft created under mandate SHOP-001")],
}
PAYOUT_NO_MANDATE: dict[str, Any] = {
    key: value
    for key, value in {**PAYOUT_DRAFT, "payoutId": PAYOUT_ID_NO_MANDATE, "reason": "Invoice refund", "history": [_entry("draft", f"{T0}.628Z", "Draft created")]}.items()
    if key != "mandateId"
}
PAYOUT_PENDING_KYC: dict[str, Any] = {
    **PAYOUT_DRAFT, "status": "pending_kyc", "updatedAt": f"{T0}.629Z",
    "history": PAYOUT_DRAFT["history"] + [_entry("pending_kyc", f"{T0}.629Z", "Submitted to KYC provider. Re-checked against mandate SHOP-001.")],
}
PAYOUT_APPROVED: dict[str, Any] = {
    **PAYOUT_PENDING_KYC, "status": "approved", "updatedAt": f"{T0}.630Z",
    "history": PAYOUT_PENDING_KYC["history"] + [_entry("approved", f"{T0}.630Z", "KYC approved by provider.")],
}
PAYOUT_PROCESSING: dict[str, Any] = {
    **PAYOUT_APPROVED, "status": "processing", "provider": "sepa-simulation", "updatedAt": f"{T0}.700Z",
    "history": PAYOUT_APPROVED["history"] + [_entry("processing", f"{T0}.700Z", "Sent to sepa-simulation from NL91ABNA0417164300 under mandate SHOP-001.")],
}
PAYOUT_PAID: dict[str, Any] = {
    **PAYOUT_PROCESSING, "status": "paid", "updatedAt": f"{T0}.733Z", "providerReference": "SIM-5DC8B5E2", "executedAt": f"{T0}.733Z",
    "history": PAYOUT_PROCESSING["history"] + [_entry("paid", f"{T0}.733Z", "Settled as sepa-simulation payment SIM-5DC8B5E2.")],
}
PAYOUT_FAILED: dict[str, Any] = {
    **PAYOUT_PROCESSING, "payoutId": PAYOUT_ID_FAILED, "amount": 20.01, "reason": "AM04 smoke", "provider": "outsourced-provider", "status": "failed",
    "createdAt": "2026-09-25T11:40:21.399Z", "updatedAt": "2026-09-25T11:40:25.834Z",
    "history": PAYOUT_PROCESSING["history"]
    + [_entry("failed", "2026-09-25T11:40:25.834Z", "Provider rejected the payment: Insufficient funds (AM04): the debtor account cannot cover 20.01 EUR.")],
}
PAYOUT_LIST_PAID: dict[str, Any] = {
    "payouts": [
        {**PAYOUT_PAID, "payoutId": PAYOUT_ID_UNRESOLVED, "amount": 20.03, "reason": "Invoice 2026-092", "history": PAYOUT_DRAFT["history"],
         "providerReference": "SIM-87118C03", "executedAt": f"{T0}.838Z"},
        {**PAYOUT_PAID, "history": PAYOUT_DRAFT["history"]},
    ]
}
SUBMISSION: dict[str, Any] = {
    "payoutId": PAYOUT_ID, "status": "pending_kyc", "provider": "outsourced-provider",
    "nextAction": "The provider answers on its own; poll the payout status until it leaves pending_kyc.",
    "message": "KYC and payment checks are outsourced to the provider.",
}
TRANSITION_KYC_APPROVED: dict[str, Any] = {
    "payoutId": PAYOUT_ID_NO_MANDATE, "status": "approved", "provider": "outsourced-provider", "message": "KYC approved by provider. Current status: approved.",
}
TRANSITION_CONFIRMED: dict[str, Any] = {
    "payoutId": PAYOUT_ID, "status": "processing", "provider": "outsourced-provider", "message": "Payout confirmation accepted. Awaiting provider settlement.",
}
EXECUTION_PAID: dict[str, Any] = {
    "payoutId": PAYOUT_ID, "status": "paid", "provider": "sepa-simulation", "environment": "SANDBOX", "providerReference": "SIM-5DC8B5E2",
    "debitedAccountIban": PAYER_IBAN, "counterpartyIban": BENEFICIARY_IBAN, "counterpartyName": "Acme Supplies BV", "amount": "-50.00",
    "currency": "EUR", "balanceAfter": "950.00", "mandateReference": "SHOP-001",
    "message": f"Payout {PAYOUT_ID} settled as sepa-simulation payment SIM-5DC8B5E2.",
}
EXECUTION_UNRESOLVED: dict[str, Any] = {
    "payoutId": PAYOUT_ID_UNRESOLVED, "status": "processing", "unresolved": True, "provider": "sepa-simulation", "environment": "SANDBOX",
    "debitedAccountIban": PAYER_IBAN, "counterpartyIban": BENEFICIARY_IBAN, "counterpartyName": "Acme Supplies BV", "amount": "-20.03",
    "currency": "EUR", "balanceAfter": None, "mandateReference": "SHOP-001",
    "message": (
        f"Payout {PAYOUT_ID_UNRESOLVED} could not be confirmed (No response from the rail within the timeout.). "
        "The payment may or may not have been sent; sepa-simulation will report which. Do not resend."
    ),
}

# Error sentences the sandbox answers with HTTP 400 (verified live).
ERROR_INSUFFICIENT_FUNDS = "Insufficient funds (AM04): the debtor account cannot cover 20.01 EUR."
ERROR_ALREADY_EXECUTED = f"Payout {PAYOUT_ID} was already executed as sepa-simulation payment SIM-5DC8B5E2. Create a new payout rather than sending it twice."
ERROR_NOT_APPROVED = f"Payout {PAYOUT_ID} must be approved (or confirmed into processing) first."
ERROR_EVENT_REFUSED = "Cannot apply event kyc_rejected when payout is in status draft. Valid from states: pending_kyc."
ERROR_PAYOUT_NOT_FOUND = "Payout nope not found."
ERROR_PAYER_NOT_FOUND = "Payer nope not found."
ERROR_MANDATE_NOT_FOUND = "Mandate nope not found."
ERROR_BENEFICIARY_NOT_FOUND = "Beneficiary nope not found."
ERROR_ROUTE_NOT_FOUND = "Not found."
ERROR_AMOUNT_DECIMALS = "Amount must have at most two decimal places."
ERROR_AMOUNT_ZERO = "Amount must be greater than zero."
ERROR_IDEMPOTENCY_MISMATCH = "This Idempotency-Key was already used with a different request body."

# --------------------------------------------------------------------------- simulation

SIMULATION: dict[str, Any] = {
    "enabled": True, "rail": "sepa-simulation", "environment": "SANDBOX", "currency": "EUR", "delayMs": 3000, "openingBalance": "1000.00",
    "triggers": {"frozenSourceIban": FROZEN_SOURCE_IBAN, "kycRejectedIban": KYC_REJECTED_IBAN},
    "scenarios": [
        {"trigger": "any amount not listed below", "outcome": "settled immediately", "payoutStatus": "paid"},
        {"trigger": "amount ending in .01", "outcome": "rejected: insufficient funds (AM04)", "payoutStatus": "failed"},
        {"trigger": "amount ending in .03", "outcome": "no confirmation within the timeout; the payment had gone through and settles after the delay",
         "payoutStatus": "processing", "then": "paid"},
        {"trigger": "amount ending in .07", "outcome": "rail unavailable; nothing was sent", "payoutStatus": "failed"},
        {"trigger": "debtor IBAN NL56SIML0000000001", "outcome": "source account frozen; refused before anything is sent", "payoutStatus": "approved"},
        {"trigger": "payee IBAN NL29SIML0000000002", "outcome": "KYC rejected after the delay", "payoutStatus": "kyc_rejected"},
    ],
    "accounts": [
        {"iban": PAYER_IBAN, "balance": "929.97", "currency": "EUR", "status": "ACTIVE"},
        {"iban": FROZEN_SOURCE_IBAN, "balance": "1000.00", "currency": "EUR", "status": "FROZEN"},
    ],
}
SIMULATION_DISABLED: dict[str, Any] = {"enabled": False, "note": "This deployment runs no simulation; payouts settle through the configured rail or not at all."}
RESET: dict[str, Any] = {"payouts": []}

# --------------------------------------------------------------------------- x402 (plain bodies)

X402_SUPPORTED: dict[str, Any] = {
    "kinds": [{"x402Version": 2, "scheme": "sepa-mandate", "network": "sepa:eu",
               "extra": {"settlement": "sepa-simulation", "simulated": True, "environment": "SANDBOX", "amountFormat": "decimal-2dp"}}],
    "extensions": [], "signers": {},
}
X402_SUPPORTED_EMPTY: dict[str, Any] = {"kinds": [], "extensions": [], "signers": {}}
X402_REQUIREMENTS: dict[str, Any] = {
    "scheme": "sepa-mandate", "network": "sepa:eu", "amount": "1.00", "asset": "EUR", "payTo": BENEFICIARY_IBAN, "maxTimeoutSeconds": 300,
    "extra": {"payeeName": "Report Vendor BV", "reference": "x402 Q3 market report"},
}
X402_PAYMENT_PAYLOAD: dict[str, Any] = {
    "x402Version": 2, "resource": {"url": "http://127.0.0.1:4604/report", "description": "Q3 market report"}, "accepted": X402_REQUIREMENTS,
    "payload": {
        "payoutId": "b1f75ef4-c754-4a58-8d18-bdbf0042e453", "mandateReference": "X402-001",
        "authorization": {"from": PAYER_IBAN, "to": BENEFICIARY_IBAN, "value": "1.00", "asset": "EUR", "validBefore": "1790191414", "nonce": "4f2c9a1e7b3d"},
        "signature": "sepa-mandate-sig-075e231d6fb6abd4736f9658e33c47741607c6b0e9722a2779607bf7f6d85069",
    },
}
X402_VERIFY_REQUEST: dict[str, Any] = {"x402Version": 2, "paymentPayload": X402_PAYMENT_PAYLOAD, "paymentRequirements": X402_REQUIREMENTS}
X402_VERIFY_VALID: dict[str, Any] = {"isValid": True, "payer": PAYER_IBAN}
X402_VERIFY_INVALID: dict[str, Any] = {"isValid": False, "invalidReason": "Authorization signature does not verify.", "payer": PAYER_IBAN}
X402_VERIFY_MALFORMED: dict[str, Any] = {"isValid": False, "invalidReason": "paymentPayload is not an object."}
X402_SETTLE_SUCCESS: dict[str, Any] = {
    "success": True, "transaction": "SIM-37F5A5A9", "network": "sepa:eu", "payer": PAYER_IBAN, "settlement": "sepa-simulation", "simulated": True,
}
X402_SETTLE_PENDING: dict[str, Any] = {
    "success": False, "network": "sepa:eu", "payer": PAYER_IBAN,
    "errorReason": "Settlement pending: the rail has not confirmed payout b1f75ef4-c754-4a58-8d18-bdbf0042e453 yet; settle again later.",
}
X402_SETTLE_MALFORMED: dict[str, Any] = {"success": False, "errorReason": "paymentPayload is not an object.", "network": "sepa:eu"}
X402_SETTLE_NO_RAIL: dict[str, Any] = {"success": False, "errorReason": "This deployment does not settle payments.", "network": "sepa:eu"}
X402_QUOTE_PAID: dict[str, Any] = {
    "url": "http://127.0.0.1:4604/report", "status": 402, "free": False, "requirement": X402_REQUIREMENTS,
    "resourceDescription": "Q3 market report", "otherOffers": [],
}
X402_QUOTE_FREE: dict[str, Any] = {"url": "https://sandbox.whire.ai/api/health", "status": 200, "free": True, "otherOffers": []}
X402_PAYMENT: dict[str, Any] = {
    "url": "http://127.0.0.1:4604/report", "paid": True, "status": 200, "payoutId": "b1f75ef4-c754-4a58-8d18-bdbf0042e453",
    "settlement": X402_SETTLE_SUCCESS, "body": '{"report":"Q3 market report"}', "contentType": "application/json",
}

# --------------------------------------------------------------------------- MCP replies (JSON-RPC ``result`` objects)

MCP_INITIALIZE_RESULT: dict[str, Any] = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True}, "completions": {}, "prompts": {"listChanged": True}},
    "serverInfo": {"name": "agent-payouts-mcp", "version": "0.1.0"},
}
MCP_RESOURCES_LIST_RESULT: dict[str, Any] = {
    "resources": [
        {"uri": "config://provider", "name": "provider-config", "title": "Deployment capabilities",
         "description": "What this deployment can do: authorization, settlement, and the provider behind them."},
        {"uri": "policy://limits", "name": "policy-limits", "title": "Payout policy",
         "description": "The mandate rules this service enforces, and the payout lifecycle guarantees."},
    ]
}
MCP_TEMPLATES_LIST_RESULT: dict[str, Any] = {
    "resourceTemplates": [
        {"name": "payout-detail-template", "uriTemplate": "payout://{payoutId}", "title": "Payout details", "description": "Retrieve payout details by ID."},
        {"name": "mandate-detail-template", "uriTemplate": "mandate://{mandateId}", "title": "Payment mandate details",
         "description": "Retrieve a payment mandate, including its limits, validity window, and status history."},
    ]
}
PROVIDER_RESOURCE_JSON: dict[str, Any] = {
    **CAPABILITIES, "provider": HEALTH["paymentProvider"], "mandates": "created and validated locally", "callbacks": "recorded locally",
}
# ``mediaType`` (not ``mimeType``) is what the sandbox sends.
MCP_RESOURCE_READ_RESULT: dict[str, Any] = {
    "contents": [{"uri": "config://provider", "text": json.dumps(PROVIDER_RESOURCE_JSON, indent=2), "mediaType": "application/json"}]
}
MCP_PROMPTS_LIST_RESULT: dict[str, Any] = {
    "prompts": [
        {"name": "run_agent_payout_flow", "title": "Run agent payout flow",
         "description": "Guide the user through the payout orchestration workflow with a payment mandate, KYC review, and provider callbacks."},
        {"name": "run_payment_mandate_flow", "title": "Run payment mandate flow",
         "description": "Show how a payment mandate authorizes payouts and how validation rejects payouts once the mandate no longer covers them."},
    ]
}
MCP_PROMPT_GET_RESULT: dict[str, Any] = {
    "messages": [{"role": "user", "content": {"type": "text", "text": (
        'Follow this sequence:\n\n1. validate_iban { "iban": "GB82 WEST 1234 5698 7654 32" }\n'
        '10. get_payout_status { "payoutId": "<PAYOUT_ID>" }\n\nUse the returned structured content to verify the sequence.'
    )}}]
}
MCP_COMPLETION_RESULT: dict[str, Any] = {"completion": {"values": [PAYOUT_ID], "total": 1, "hasMore": False}}

# The three ``isError`` families of ``tools/call`` (verified live).
MCP_ERROR_UNKNOWN_TOOL = "MCP error -32602: Tool nope not found"
MCP_ERROR_INVALID_ARGUMENTS = (
    "MCP error -32602: Input validation error: Invalid arguments for tool get_payer: [\n  {\n"
    '    "expected": "string",\n    "code": "invalid_type",\n    "path": [\n      "payerId"\n    ],\n'
    '    "message": "Invalid input: expected string, received number"\n  }\n]'
)
MCP_ERROR_ARGUMENTS_MISSING = (
    "MCP error -32602: Input validation error: Invalid arguments for tool list_payers: [\n  {\n"
    '    "expected": "object",\n    "code": "invalid_type",\n    "path": [],\n'
    '    "message": "Invalid input: expected object, received undefined"\n  }\n]'
)
MCP_ERROR_BUSINESS_NOT_FOUND = "Payer nope not found."
MCP_ERROR_BUSINESS_EXECUTED = ERROR_ALREADY_EXECUTED

MCP_JSONRPC_PARSE_ERROR: dict[str, Any] = {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error: Invalid JSON"}, "id": None}
MCP_JSONRPC_METHOD_NOT_FOUND: dict[str, Any] = {"code": -32601, "message": "Method not found"}
MCP_JSONRPC_RESOURCE_MISSING: dict[str, Any] = {"code": -32603, "message": "Payout nope not found."}
MCP_JSONRPC_UNKNOWN_SCHEME: dict[str, Any] = {"code": -32602, "message": "Resource nope://x not found"}
MCP_JSONRPC_NOT_ACCEPTABLE: dict[str, Any] = {"code": -32000, "message": "Not Acceptable: Client must accept both application/json and text/event-stream"}

# Tool -> canned structuredContent (single-object tools) or list wrapper (list tools).
TOOL_RESULTS: dict[str, dict[str, Any]] = {
    "register_payer": PAYER_PENDING, "activate_payer_account": PAYER_ACTIVE, "suspend_payer_account": PAYER_SUSPENDED,
    "add_funding_source": PAYER_ACTIVE, "set_default_funding_source": PAYER_ACTIVE, "get_payer": PAYER_ACTIVE, "list_payers": PAYERS_LIST,
    "authorize_payment": RECEIPT, "verify_authorization_receipt": RECEIPT_VERIFICATION, "get_capabilities": CAPABILITIES,
    "register_user": USER, "add_payment_method": USER, "set_default_payment_method": USER, "get_user": USER, "list_users": USERS_LIST,
    "create_beneficiary_for_user": BENEFICIARY_FOR_USER, "validate_iban": IBAN_VALIDATION, "create_beneficiary": BENEFICIARY,
    "get_beneficiary": BENEFICIARY, "create_payment_mandate": MANDATE, "validate_payment_mandate": MANDATE_VALIDATION,
    "revoke_payment_mandate": MANDATE_REVOKED, "list_payment_mandates": MANDATES_LIST, "create_payout_draft": PAYOUT_DRAFT,
    "submit_payout_for_review": SUBMISSION, "record_provider_event": TRANSITION_KYC_APPROVED, "confirm_payout": TRANSITION_CONFIRMED,
    "execute_payout": EXECUTION_PAID, "get_payout_status": PAYOUT_APPROVED, "list_payouts": PAYOUT_LIST_PAID, "get_simulation": SIMULATION,
    "quote_x402_resource": X402_QUOTE_PAID, "pay_x402_resource": X402_PAYMENT,
}


def fresh(payload: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of a canned payload, safe to mutate."""
    return copy.deepcopy(payload)
