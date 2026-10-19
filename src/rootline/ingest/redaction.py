import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from rootline.ingest.document_builder import DocBuilder
from rootline.models import ParsedDoc

SEPARATORS = re.compile(r"[-_/.=+]")
PLACEHOLDER = re.compile(r"\[REDACTED:[A-Z_]+\]")


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0
    accept: Callable[[str], bool] = lambda value: True


@dataclass(frozen=True)
class Redaction:
    text: str
    # One entry per secret found. Only the kind is kept, never the value.
    kinds: list[str] = field(default_factory=list)


def looks_secret(value: str) -> bool:
    # Keeps "password reset" or "token: none" from being treated as secrets.
    return len(value) >= 6 and any(not char.isalpha() for char in value)


def high_entropy(value: str) -> bool:
    # Judge the longest unbroken run: a slug like "inc-2025-06-03-orders-api-outage" is made of
    # short words, while a real token is one long random run.
    run = max(SEPARATORS.split(value), key=len)
    if len(run) < 16 or run.isalpha() or run.isdigit():
        return False
    counts = Counter(run)
    entropy = -sum(n / len(run) * math.log2(n / len(run)) for n in counts.values())
    return entropy >= 3.5


# Order matters: specific formats first, so a generic rule never claims part of a key that a
# specific rule would have labelled better.
RULES = [
    Rule(
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
    ),
    Rule("JWT", re.compile(r"\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+")),
    Rule("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("GOOGLE_API_KEY", re.compile(r"\bAIza[\w-]{35}")),
    Rule("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    Rule("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    Rule("API_KEY", re.compile(r"\b(?:sk|pk|rk)_[A-Za-z0-9_]{16,}")),
    Rule("TOKEN", re.compile(r"(?i)\bbearer\s+([\w.~+/-]{16,}=*)"), group=1),
    Rule("PASSWORD", re.compile(r"://[^/\s:@]+:([^@\s/]+)@"), group=1),
    Rule(
        "TOKEN",
        re.compile(r"(?i)[?&](?:token|key|sig|signature|password|secret|access_token)=([^&\s#]+)"),
        group=1,
    ),
    Rule(
        "SECRET",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|access[_-]?key|client[_-]?secret|auth[_-]?token)\b"
            r"\s*[:=]\s*['\"]?([^\s'\",;&]+)"
        ),
        group=1,
        accept=looks_secret,
    ),
    Rule(
        "PASSWORD",
        re.compile(r"(?i)\b(?:password|passwd|pwd)\b\s*(?:[:=]|\bis\b)?\s*['\"]?([^\s'\",;]+)"),
        group=1,
        accept=looks_secret,
    ),
    Rule(
        "INTERNAL_HOST",
        re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+(?:internal|local|corp|lan|intra|intranet|private)\b"),
    ),
    Rule(
        "PRIVATE_IP",
        re.compile(
            r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
            r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
        ),
    ),
    Rule("HIGH_ENTROPY", re.compile(r"[A-Za-z0-9+/_=-]{24,}"), accept=high_entropy),
]


def placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact(text: str) -> Redaction:
    kinds: list[str] = []
    for rule in RULES:
        text = apply(rule, text, kinds)
    return Redaction(text, kinds)


def apply(rule: Rule, text: str, kinds: list[str]) -> str:
    # A later rule must not match inside a placeholder an earlier rule wrote, for example the
    # word PASSWORD in "[REDACTED:PASSWORD]".
    written = [match.span() for match in PLACEHOLDER.finditer(text)]

    def replace(match: re.Match[str]) -> str:
        overlaps = any(start < match.end() and match.start() < end for start, end in written)
        if overlaps or not rule.accept(match.group(rule.group)):
            return match.group(0)
        kinds.append(rule.kind)
        start, end = match.start(rule.group) - match.start(), match.end(rule.group) - match.start()
        whole = match.group(0)
        return whole[:start] + placeholder(rule.kind) + whole[end:]

    return rule.pattern.sub(replace, text)


def redact_doc(doc: ParsedDoc) -> tuple[ParsedDoc, list[str]]:
    kinds: list[str] = []

    def clean(text: str) -> str:
        result = redact(text)
        kinds.extend(result.kinds)
        return result.text

    builder = DocBuilder()
    for block in doc.blocks:
        if block.kind == "table":
            builder.add_table([[clean(cell) for cell in row] for row in block.rows])
        else:
            builder.add(block.kind, clean(block.text), block.level)
    # Fields repeat values already counted in the blocks, so they are redacted without counting.
    fields = {
        key: redact(value).text if isinstance(value, str) else value
        for key, value in doc.fields.items()
    }
    redacted = builder.build(doc.source_key, doc.source_format, fields, title=clean(doc.title))
    return redacted, kinds
