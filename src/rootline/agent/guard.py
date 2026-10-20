import re
import unicodedata

from pydantic import BaseModel

from rootline.core.text import CONTROL
from rootline.ingest.redaction import redact

MAX_BYTES = 20_000
# A long paste keeps its head and its tail: Java prints the exception first, Python last.
TAIL_BYTES = 4_000
INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")
# Flagged and logged only. The text still goes on as plain data, and nothing downstream
# lets a message run commands or change instructions.
INJECTION = {
    "override": re.compile(
        r"(?i)\b(?:ignore|disregard|forget)\b.{0,40}\b(?:previous|prior|above|earlier|all)\b"
        r".{0,20}\b(?:instructions?|rules|prompts?)\b"
    ),
    "role_tag": re.compile(
        r"(?im)<\|?\s*(?:system|assistant|im_start)\s*\|?>|^\s*(?:system|assistant)\s*:"
    ),
    "prompt_leak": re.compile(
        r"(?i)\b(?:reveal|show|print|repeat)\b.{0,20}"
        r"\b(?:system prompt|your (?:instructions|prompt|rules))"
    ),
    "persona": re.compile(
        r"(?i)\byou are now\b|\bdeveloper mode\b|\bjailbreak\b"
        r"|\bact as (?:an? )?(?:admin|root|dan)\b"
    ),
}


class GuardReport(BaseModel):
    truncated: bool = False
    redacted: list[str] = []
    injection: list[str] = []


class Guarded(BaseModel):
    text: str
    report: GuardReport


def guard(text: str) -> Guarded:
    text, truncated = cap(text)
    # NFKC first, so full-width or zero-width tricks cannot hide a flagged phrase.
    text = CONTROL.sub("", INVISIBLE.sub("", unicodedata.normalize("NFKC", text)))
    redaction = redact(text)
    injection = [name for name, rule in INJECTION.items() if rule.search(redaction.text)]
    return Guarded(
        text=redaction.text,
        report=GuardReport(truncated=truncated, redacted=redaction.kinds, injection=injection),
    )


def cap(text: str) -> tuple[str, bool]:
    data = text.encode()
    if len(data) <= MAX_BYTES:
        return text, False
    head = data[: MAX_BYTES - TAIL_BYTES].decode(errors="ignore")
    tail = data[-TAIL_BYTES:].decode(errors="ignore")
    return f"{head}\n[{len(data) - MAX_BYTES} bytes cut]\n{tail}", True
