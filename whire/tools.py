"""The 33 tool definitions served by the deployment's MCP endpoint, plus one SDK-only helper.

``TOOLS`` is loaded from ``whire/_tools_data.json``, a verbatim copy of the
sandbox's ``tools/list`` answer (regenerated with
``scratchpad/gen_tools_data.py``). Names, titles, descriptions, property
names, required lists, enums, output schemas, annotations and execution
metadata are exactly the server's. The only addition is a short factual
``description`` on each input property (``PROPERTY_DESCRIPTIONS``), taken from
https://sandbox.whire.ai/docs, so that function-calling models get a hint the
server does not ship.

Every entry has the shape ``{"name", "title", "description", "input_schema",
"output_schema", "annotations" (dict or None), "execution"}``. Treat the
structures as read-only: :meth:`whire.toolkit.WhireToolkit.get_tools`
deep-copies before handing them out.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from typing import Any

from whire.mcp_client import READ_ONLY_TOOLS

__all__ = [
    "TOOLS",
    "TOOLS_BY_NAME",
    "TOOL_NAMES",
    "READ_ONLY_TOOLS",
    "DESTRUCTIVE_TOOLS",
    "LIST_TOOL_WRAPPERS",
    "AMOUNT_PROPERTIES",
    "PROPERTY_DESCRIPTIONS",
    "WAIT_FOR_PAYOUT_TOOL",
    "HELPER_TOOLS",
    "get_tool",
]

_DATA_FILE = "_tools_data.json"

# Wire names of the number-typed input properties that are money amounts (positive, at most two decimals).
AMOUNT_PROPERTIES: frozenset[str] = frozenset({"amount", "maxAmount", "maxTotalAmount"})

# The four tools whose result wraps a list: tool name → wrapper key.
LIST_TOOL_WRAPPERS: dict[str, str] = {
    "list_payers": "payers",
    "list_users": "users",
    "list_payment_mandates": "mandates",
    "list_payouts": "payouts",
}

# ---------------------------------------------------------------------- per-property descriptions (from the docs)

_PAYER_ID = "Id of the payer (from register_payer / list_payers)."
_PAYOUT_ID = "Id of the payout (from create_payout_draft / list_payouts)."
_MANDATE_ID = "Id of the mandate (from create_payment_mandate / list_payment_mandates)."
_BENEFICIARY_ID = "Id of the beneficiary (from create_beneficiary / create_beneficiary_for_user)."
_USER_ID = "Id of the registered person (from register_user / list_users)."
_AMOUNT = "Amount as a JSON number, greater than zero, at most two decimals (EUR today)."
_CURRENCY = "3-letter ISO currency code; upper-cased by the server. EUR only today."
_SOURCE_TYPE = "'sepa' (destination is an IBAN) or 'x402_wallet' (destination is a 0x-prefixed wallet address)."
_DESTINATION = "IBAN (validated including its checksum) or 0x-prefixed wallet address."
_HOLDER_NAME = "Name of the account holder; defaults to the owner's name."
_LABEL = "Free-text label; defaults to 'Bank account <last 4 digits>'."
_ISO_DATETIME = "ISO-8601 date or datetime (UTC, e.g. 2026-09-23T18:42:40.612Z or 2026-09-23)."

PROPERTY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "register_payer": {
        "legalName": "Legal name of the customer whose money will move.",
        "accountType": "'business' or 'individual'.",
        "registrationNumber": "Company registration number; recorded, not checked against any registry.",
        "vatNumber": "VAT number; recorded, not checked against any registry.",
        "contactFirstName": "First name of the contact person.",
        "contactLastName": "Last name of the contact person.",
        "email": "Contact email; an email can be registered for one payer only.",
        "phone": "Contact phone in international format (+countrycode...).",
        "fundingSources": (
            "At least one account to debit ({type, destination, holderName?, label?, makeDefault?}); "
            "the first becomes the default. A funding-source IBAN belongs to at most one payer."
        ),
    },
    "activate_payer_account": {
        "payerId": _PAYER_ID,
        "verifiedBy": "Name of the person who checked the verification documents (a human decision).",
        "note": "Free-text note written into the payer's history.",
    },
    "suspend_payer_account": {
        "payerId": _PAYER_ID,
        "reason": "Why the payer is suspended; written into the history. Existing mandates stay until revoked.",
    },
    "add_funding_source": {
        "payerId": _PAYER_ID,
        "type": _SOURCE_TYPE,
        "destination": _DESTINATION + " The same IBAN cannot be added twice to a payer.",
        "holderName": _HOLDER_NAME,
        "label": _LABEL,
        "makeDefault": "true to make this the source mandates debit by default.",
    },
    "set_default_funding_source": {
        "payerId": _PAYER_ID,
        "sourceId": "sourceId of one of the payer's funding sources.",
    },
    "get_payer": {"payerId": _PAYER_ID},
    "authorize_payment": {
        "mandateId": _MANDATE_ID,
        "amount": _AMOUNT,
        "currency": _CURRENCY + " Defaults to the mandate's currency.",
        "beneficiaryId": "Payee as a beneficiary id (the reliable path). Either beneficiaryId or payee is required.",
        "payee": "Payee as an IBAN. Either beneficiaryId or payee is required.",
        "payeeName": "Payee name recorded on the receipt when paying by IBAN.",
    },
    "verify_authorization_receipt": {
        "receipt": (
            "The receipt object exactly as authorize_payment returned it (same strings for amount, "
            "issuedAt and expiresAt); any edit makes signatureValid false."
        ),
    },
    "register_user": {
        "firstName": "First name of the person who will receive payouts.",
        "lastName": "Last name.",
        "email": "Email address.",
        "phone": "Phone in international format (+countrycode...).",
        "paymentMethods": (
            "At least one way to pay them ({type, destination, holderName?, label?, makeDefault?}); "
            "the first becomes the default."
        ),
    },
    "add_payment_method": {
        "userId": _USER_ID,
        "type": _SOURCE_TYPE,
        "destination": _DESTINATION,
        "holderName": _HOLDER_NAME,
        "label": _LABEL,
        "makeDefault": "true to make this the method payouts use by default.",
    },
    "set_default_payment_method": {
        "userId": _USER_ID,
        "methodId": "methodId of one of the person's payment methods.",
    },
    "get_user": {"userId": _USER_ID},
    "create_beneficiary_for_user": {
        "userId": _USER_ID,
        "methodId": "Payment method to use; defaults to the person's default method. x402_wallet methods are refused on a SEPA deployment.",
    },
    "validate_iban": {
        "iban": "IBAN to check; spaces are ignored and the result carries the normalized form.",
        "countryCode": "Expected 2-letter country code; the check fails when the IBAN's country differs.",
    },
    "create_beneficiary": {
        "fullName": "Name of the payee.",
        "iban": "Payee IBAN (validated including its checksum). Every call creates a new record.",
        "reference": "Your reference for this payee.",
        "email": "Payee email.",
        "walletAddress": "0x-prefixed wallet address; accepted but not returned today.",
    },
    "get_beneficiary": {"beneficiaryId": _BENEFICIARY_ID},
    "create_payment_mandate": {
        "beneficiaryId": "The only payee this mandate allows.",
        "mandateReference": (
            "Your reference; SEPA rules: 1-35 characters from letters, digits, space and +?/-:().,' "
            "with no leading or trailing '/' and no '//'."
        ),
        "payerId": "Verified (active) payer whose default funding source is debited; preferred over debtorName/debtorIban.",
        "fundingSourceId": "One of the payer's funding sources to debit instead of the default; not echoed back (read debtorIban).",
        "debtorName": "Debtor name when no payerId is given (send together with debtorIban).",
        "debtorIban": "Debtor IBAN when no payerId is given (send together with debtorName).",
        "signedBy": "Who signed the mandate.",
        "currency": _CURRENCY + " The only currency the mandate allows.",
        "maxAmount": "Cap per payment. " + _AMOUNT,
        "maxTotalAmount": "Optional cap on the sum of all payments. " + _AMOUNT,
        "scheme": "'sepa_core', 'sepa_b2b' or 'agent_payout'.",
        "mandateType": "'one_off' (a single payment) or 'recurring'.",
        "rail": "'sepa' or 'x402'. The service currently records every mandate on the SEPA rail and ignores this field; read the returned mandate's rail.",
        "validFrom": "Start of the validity window; " + _ISO_DATETIME,
        "validUntil": "End of the validity window, at most 1095 days after validFrom; " + _ISO_DATETIME,
        "signedAt": "When it was signed (must not be after validUntil); " + _ISO_DATETIME,
    },
    "validate_payment_mandate": {
        "mandateId": _MANDATE_ID,
        "amount": "Planned payment amount to check against the limits. " + _AMOUNT,
        "currency": "Planned payment currency to check against the mandate's.",
        "beneficiaryId": "Planned payee to check against the mandate's beneficiary.",
        "payoutId": "Existing payout to re-check against its mandate instead of amount/currency/beneficiaryId.",
    },
    "revoke_payment_mandate": {
        "mandateId": _MANDATE_ID,
        "reason": "Why it is revoked; written into the mandate's history.",
    },
    "create_payout_draft": {
        "beneficiaryId": _BENEFICIARY_ID,
        "amount": _AMOUNT,
        "currency": _CURRENCY,
        "reason": "What the payout is for (e.g. an invoice reference).",
        "mandateId": "Mandate that must authorize this payout; its ten checks run on submit and execute.",
    },
    "submit_payout_for_review": {"payoutId": _PAYOUT_ID + " Must be a draft."},
    "record_provider_event": {
        "payoutId": _PAYOUT_ID,
        "event": (
            "Provider decision to apply: kyc_approved, kyc_rejected, payment_processing, payment_paid, "
            "payment_failed or payment_returned. Refused when it does not fit the current status."
        ),
    },
    "confirm_payout": {"payoutId": _PAYOUT_ID + " Must be approved; moves it to processing without settling."},
    "execute_payout": {"payoutId": _PAYOUT_ID + " Must be approved or processing (after confirm)."},
    "get_payout_status": {"payoutId": _PAYOUT_ID},
    "quote_x402_resource": {"url": "URL of the resource; a 402 answer is read and reported, nothing is paid."},
    "pay_x402_resource": {
        "url": "URL of the 402-gated resource to pay for and fetch.",
        "mandateId": _MANDATE_ID + " Must cover the quoted amount and payee.",
        "reason": "Reason recorded on the payout created for this payment.",
    },
}


# ---------------------------------------------------------------------- loading


def _load_server_tools() -> list[dict[str, Any]]:
    text = resources.files(__package__).joinpath(_DATA_FILE).read_text(encoding="utf-8")
    tools = json.loads(text)
    if not isinstance(tools, list):
        raise RuntimeError(f"{_DATA_FILE} must hold a list of tools")
    return tools


def _describe(schema: dict[str, Any], descriptions: dict[str, str], tool: str) -> None:
    properties = schema.get("properties") or {}
    unknown = set(descriptions) - set(properties)
    if unknown:
        raise RuntimeError(f"PROPERTY_DESCRIPTIONS[{tool!r}] names undeclared properties: {sorted(unknown)}")
    for name, text in descriptions.items():
        properties[name]["description"] = text


def _to_entry(server_tool: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": server_tool["name"],
        "title": server_tool.get("title"),
        "description": server_tool.get("description"),
        "input_schema": copy.deepcopy(server_tool["inputSchema"]),
        "output_schema": copy.deepcopy(server_tool.get("outputSchema")),
        "annotations": copy.deepcopy(server_tool.get("annotations")),
        "execution": copy.deepcopy(server_tool.get("execution")),
    }
    _describe(entry["input_schema"], PROPERTY_DESCRIPTIONS.get(entry["name"], {}), entry["name"])
    return entry


TOOLS: list[dict[str, Any]] = [_to_entry(tool) for tool in _load_server_tools()]
"""The 33 server tools in the server's order."""

TOOLS_BY_NAME: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in TOOLS}
TOOL_NAMES: tuple[str, ...] = tuple(tool["name"] for tool in TOOLS)

DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    tool["name"] for tool in TOOLS if (tool["annotations"] or {}).get("destructiveHint") is True
)
"""Tools whose server annotations carry ``destructiveHint: true`` (they move money)."""

if DESTRUCTIVE_TOOLS & READ_ONLY_TOOLS:
    raise RuntimeError("a tool cannot be both destructive and read-only")

# ---------------------------------------------------------------------- SDK-only helper tool

WAIT_FOR_PAYOUT_TOOL: dict[str, Any] = {
    "name": "wait_for_payout",
    "title": "Wait for a payout to change status",
    "description": (
        "SDK helper, not offered by the hosted MCP server: poll get_payout_status until the payout reaches one of "
        "untilStatuses (default: until it leaves pending_kyc and processing) or timeoutSeconds elapse, then return "
        "the full payout object. Polling never moves money. On timeout an error with error_code 'timeout' and the "
        "last payout read is returned; poll again later instead of repeating the action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "payoutId": {"type": "string", "description": _PAYOUT_ID},
            "untilStatuses": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["draft", "pending_kyc", "kyc_rejected", "approved", "processing", "paid", "failed", "returned"],
                },
                "description": "Return as soon as the payout is in one of these statuses.",
            },
            "timeoutSeconds": {
                "type": "number",
                "maximum": 60,
                "description": "How long to poll at most (default 30, capped at 60).",
            },
        },
        "required": ["payoutId"],
    },
    "output_schema": copy.deepcopy(TOOLS_BY_NAME["get_payout_status"]["output_schema"]),
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    "execution": None,
}

HELPER_TOOLS: list[dict[str, Any]] = [WAIT_FOR_PAYOUT_TOOL]
"""Tools the SDK adds on request (``get_tools(include_helpers=True)``); absent from the hosted server."""

HELPER_TOOLS_BY_NAME: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in HELPER_TOOLS}


def get_tool(name: str) -> dict[str, Any]:
    """Return the definition of a server tool or helper tool (``KeyError`` when unknown)."""
    try:
        return TOOLS_BY_NAME[name]
    except KeyError:
        return HELPER_TOOLS_BY_NAME[name]
