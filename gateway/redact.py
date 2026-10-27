"""Mascarea datelor personale înainte ca textul să plece la providerul LLM.

Fiecare valoare găsită e înlocuită cu un placeholder stabil (`[EMAIL_1]`,
`[PHONE_1]`, ...). Maparea rămâne în gateway, așa că în răspunsul modelului
placeholderele pot fi puse înapoi. Providerul nu vede niciodată datele reale,
dar utilizatorul primește un răspuns complet.

Detectăm: e-mail, telefon (Moldova și internațional), card bancar (cu
verificare Luhn, ca să nu mascăm orice număr lung), IDNP (13 cifre, începe
cu 0 sau 2) și IBAN MD.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("IBAN", re.compile(r"\bMD\d{2}[A-Z0-9]{20}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("IDNP", re.compile(r"\b[02]\d{12}\b")),
    # internațional (+ sau 00) ori local cu 0 în față; numărul de cifre se verifică în `_phone_ok`
    ("PHONE", re.compile(r"(?<![\w+])(?:(?:\+|00)\d[\d ()-]{7,17}\d|\(?0[2-7]\d\)?[ -]?\d[\d -]{4,9}\d)\b")),
]


def luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _phone_ok(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if value.lstrip("(").startswith("0") and not value.startswith("00"):
        return len(digits) == 9          # 069123456, 022212121
    return 10 <= len(digits) <= 15       # +373..., +40..., 00373...


@dataclass
class Redaction:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.mapping)

    def restore(self, text: str) -> str:
        # cele mai lungi primele, ca [EMAIL_10] să nu fie stricat de [EMAIL_1]
        for placeholder in sorted(self.mapping, key=len, reverse=True):
            text = text.replace(placeholder, self.mapping[placeholder])
        return text


def redact(text: str, existing: Redaction | None = None) -> Redaction:
    """`existing` permite aceeași mapare pe mai multe mesaje ale unei conversații."""
    result = Redaction(text, dict(existing.mapping) if existing else {})
    reverse = {v: k for k, v in result.mapping.items()}
    counters: dict[str, int] = {}
    for placeholder in result.mapping:
        kind, n = placeholder.strip("[]").rsplit("_", 1)
        counters[kind] = max(counters.get(kind, 0), int(n))

    def replace(kind: str, match: re.Match[str]) -> str:
        value = match.group(0)
        if kind == "CARD" and not luhn_ok(value):
            return value
        if kind == "PHONE" and not _phone_ok(value):
            return value
        if value in reverse:
            return reverse[value]
        counters[kind] = counters.get(kind, 0) + 1
        placeholder = f"[{kind}_{counters[kind]}]"
        result.mapping[placeholder] = value
        reverse[value] = placeholder
        return placeholder

    out = text
    for kind, pattern in PATTERNS:
        out = pattern.sub(lambda m, k=kind: replace(k, m), out)
    result.text = out
    return result
