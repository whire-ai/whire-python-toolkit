"""Per-run unique test data for the live suite.

The sandbox is a shared store with undocumented uniqueness rules (a funding
IBAN belongs to one payer, an email registers once, a beneficiary IBAN is
reused store-wide by ``create_beneficiary_for_user``). Everything created by a
live run therefore carries a fresh random IBAN, email or reference, and the
docs' sample IBANs are never used for payers.
"""

from __future__ import annotations

import random
import string
import uuid

__all__ = [
    "RUN_ID",
    "iban_checksum_valid",
    "random_iban",
    "unique_email",
    "unique_reference",
    "unique_text",
]

RUN_ID: str = uuid.uuid4().hex[:8]
"""Eight hex characters identifying this process's run; embedded in every name."""

_SAMPLE_IBANS: frozenset[str] = frozenset(
    {
        "NL91ABNA0417164300",  # docs' payer sample
        "DE89370400440532013000",  # docs' beneficiary sample
        "NL56SIML0000000001",  # frozen-source trigger
        "NL29SIML0000000002",  # kyc-rejected trigger
    }
)

_rng = random.SystemRandom()


def _mod97(text: str) -> int:
    digits = "".join(str(int(ch, 36)) for ch in text)
    return int(digits) % 97


def iban_checksum_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check on a normalized IBAN."""
    normalized = "".join(iban.split()).upper()
    if len(normalized) < 15 or not normalized[:2].isalpha() or not normalized[2:4].isdigit():
        return False
    return _mod97(normalized[4:] + normalized[:4]) == 1


def random_iban(country: str = "NL", bank: str = "ABNA", account_len: int = 10) -> str:
    """A syntactically valid random IBAN whose check digits satisfy mod-97.

    Defaults produce a Dutch ABNA-style IBAN (18 chars). ``random_iban("DE",
    "37040044", 10)`` produces a German one (22 chars). Never returns one of the
    docs' sample IBANs or a simulation trigger.
    """
    while True:
        account = "".join(_rng.choices(string.digits, k=account_len))
        bban = bank + account
        check = 98 - _mod97(bban + country + "00")
        iban = f"{country}{check:02d}{bban}"
        if iban in _SAMPLE_IBANS:
            continue
        assert iban_checksum_valid(iban), iban
        return iban


def unique_text(prefix: str = "live") -> str:
    """``<prefix>-<run>-<6 hex>``: unique across runs and within one run."""
    return f"{prefix}-{RUN_ID}-{uuid.uuid4().hex[:6]}"


def unique_email(prefix: str = "live") -> str:
    """An email address never registered before (payer and user emails are unique)."""
    return f"{unique_text(prefix)}@example.com"


def unique_reference(prefix: str = "LIVE") -> str:
    """A mandate reference (1-35 chars, ``A-Za-z0-9 +?/-:().,'``) unique per call."""
    reference = f"{prefix}-{RUN_ID}-{uuid.uuid4().hex[:6]}".upper()
    assert len(reference) <= 35, reference
    return reference
