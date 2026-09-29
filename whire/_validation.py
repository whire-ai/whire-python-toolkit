"""Local validation helpers shared by the client, the toolkit and the tests.

Everything here raises :class:`~whire.exceptions.InvalidInputError` before a
request is sent, so a refused value never reaches the wire.
"""

from __future__ import annotations

import ipaddress
import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, TypeVar
from urllib.parse import urlsplit

from whire.exceptions import AuthenticationError, InvalidInputError

__all__ = [
    "MANDATE_REFERENCE_MAX_LENGTH",
    "MAX_MANDATE_DURATION_DAYS",
    "to_amount",
    "amount_to_json",
    "to_iso_datetime",
    "validate_mandate_reference",
    "normalize_iban",
    "iban_shape_valid",
    "iban_checksum_valid",
    "enum_value",
    "validated_enum",
    "require_str",
    "require_id",
    "upper_currency",
]

MANDATE_REFERENCE_MAX_LENGTH = 35
MAX_MANDATE_DURATION_DAYS = 1095
# The server parses IEEE doubles and amount_to_json sends a float. A two-decimal amount below
# 1e13 has at most 15 significant digits, which a double always round-trips exactly; larger
# amounts can lose the last cent (99999999999999.99 -> 99999999999999.98), so they are refused.
MAX_JSON_AMOUNT = Decimal("1e13")

_MANDATE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9 +?/\-:().,']+$")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_IBAN_SHAPE_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")

PRODUCTION_HOST = "api.whire.ai"

E = TypeVar("E", bound=Enum)


def to_amount(value: Any, *, field: str = "amount") -> Decimal:
    """Validate a money amount: positive, finite, at most two decimals.

    Accepts ``Decimal``, ``int``, ``float`` (via its shortest repr) and
    numeric ``str``; rejects ``bool``, NaN, infinities, ``<= 0`` and more than
    two decimal places.
    """
    if isinstance(value, bool) or value is None:
        raise InvalidInputError(f"{field} must be a number, not {type(value).__name__}")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise InvalidInputError(f"{field} must be a finite number")
        amount = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation:
            raise InvalidInputError(f"{field} is not a valid decimal string: {value!r}") from None
    else:
        raise InvalidInputError(f"{field} must be a number, not {type(value).__name__}")
    if not amount.is_finite():
        raise InvalidInputError(f"{field} must be a finite number")
    if amount <= 0:
        raise InvalidInputError(f"{field} must be greater than zero")
    if amount >= MAX_JSON_AMOUNT:
        raise InvalidInputError(f"{field} is too large to send exactly as a JSON number (must be below 1e13)")
    if -amount.as_tuple().exponent > 2:
        raise InvalidInputError(f"{field} must have at most two decimal places")
    return amount


def amount_to_json(amount: Decimal) -> int | float:
    """Return the JSON number the API expects for a validated amount."""
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def to_iso_datetime(value: datetime | date | str, *, field: str) -> str:
    """Return the wire form of a date or datetime.

    Aware datetimes become UTC ISO-8601 with milliseconds (``…Z``); naive
    datetimes are refused; ``date`` becomes ``YYYY-MM-DD``; strings must parse
    as ISO-8601 and are sent as given.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidInputError(f"{field} must be timezone-aware (naive datetimes are ambiguous)")
        utc = value.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidInputError(f"{field} must not be empty")
        if not _parses_as_iso(text):
            raise InvalidInputError(f"{field} must be an ISO-8601 date or datetime string, got {value!r}")
        return text
    raise InvalidInputError(f"{field} must be a datetime, date or ISO-8601 string")


def _parses_as_iso(text: str) -> bool:
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    for parser in (datetime.fromisoformat, date.fromisoformat):
        try:
            parser(candidate)
            return True
        except ValueError:
            continue
    return False


def validate_mandate_reference(reference: str) -> str:
    """Validate a SEPA mandate reference and return it unchanged.

    1–35 characters from ``A-Za-z0-9 +?/-:().,'``, no leading or trailing
    ``/`` and no ``//``.
    """
    if not isinstance(reference, str):
        raise InvalidInputError("mandate_reference must be a string")
    if not 1 <= len(reference) <= MANDATE_REFERENCE_MAX_LENGTH:
        raise InvalidInputError(
            f"mandate_reference must be 1-{MANDATE_REFERENCE_MAX_LENGTH} characters, got {len(reference)}"
        )
    if not _MANDATE_REFERENCE_RE.match(reference):
        raise InvalidInputError(
            "mandate_reference may only contain letters, digits, space and +?/-:().,'"
        )
    if reference.startswith("/") or reference.endswith("/") or "//" in reference:
        raise InvalidInputError("mandate_reference must not start or end with '/' or contain '//'")
    return reference


def normalize_iban(iban: str) -> str:
    """Strip spaces and upper-case an IBAN (no validation)."""
    if not isinstance(iban, str):
        raise InvalidInputError("iban must be a string")
    return "".join(iban.split()).upper()


def iban_shape_valid(iban: str) -> bool:
    """Whether the normalized IBAN has a plausible shape (country, check digits, length)."""
    return bool(_IBAN_SHAPE_RE.match(normalize_iban(iban)))


def iban_checksum_valid(iban: str) -> bool:
    """Whether the IBAN passes the ISO 13616 mod-97 check."""
    normalized = normalize_iban(iban)
    if not _IBAN_SHAPE_RE.match(normalized):
        return False
    rearranged = normalized[4:] + normalized[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def enum_value(value: Enum | str | None) -> str | None:
    """Return the wire string of an enum member or string (``None`` passes through)."""
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def validated_enum(value: Enum | str, enum_cls: type[E], *, field: str) -> str:
    """Return the wire string of ``value``, refusing values unknown to ``enum_cls``."""
    text = enum_value(value)
    allowed = [str(member.value) for member in enum_cls]
    if text not in allowed:
        raise InvalidInputError(f"{field} must be one of {allowed}, got {value!r}")
    return text


def require_str(value: Any, *, field: str) -> str:
    """Require a non-empty string (ids, names, references)."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{field} must be a non-empty string")
    return value


def require_id(value: Any, *, field: str) -> str:
    """Require a record id (UUID, ``auth_<hex>``, ...) safe to place in a URL path.

    Only letters, digits, ``.``, ``_``, ``:`` and ``-`` are accepted, starting
    with a letter or digit, so ``/``, ``?``, ``#``, whitespace and ``..``
    segments can never reach the request path.
    """
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise InvalidInputError(f"{field} must be a record id (letters, digits, '.', '_', ':', '-'), got {value!r}")
    return value


def upper_currency(currency: str | None, *, default: str | None = None) -> str | None:
    """Upper-case a currency code (the server does the same); ``None`` → ``default``."""
    if currency is None:
        return default
    text = require_str(currency, field="currency").strip().upper()
    if len(text) != 3 or not text.isalpha():
        raise InvalidInputError(f"currency must be a 3-letter ISO code, got {currency!r}")
    return text


# ---------------------------------------------------------------------- base URL / key checks
# Shared by WhireClient and the standalone WhireMCPClient.


def is_production_host(host: str | None) -> bool:
    """Whether ``host`` names the production deployment (case-insensitive, trailing dot ignored)."""
    return (host or "").lower().rstrip(".") == PRODUCTION_HOST


def _validate_base_url(base_url: str, allow_insecure_http: bool) -> str:
    text = base_url.strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidInputError(f"base_url must be an http(s) URL with a host, got {base_url!r}")
    if parts.scheme == "http" and not allow_insecure_http and not _is_local_host(parts.hostname):
        raise InvalidInputError(
            f"base_url {base_url!r} uses plain http to a non-local host; use https or allow_insecure_http=True"
        )
    return text


def _is_local_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _check_production_key(base_url: str, key: str | None, allow_unauthenticated: bool) -> None:
    host = urlsplit(base_url).hostname
    if key is None and is_production_host(host) and not allow_unauthenticated:
        raise AuthenticationError("api_key is required for production; set WHIRE_API_KEY")
