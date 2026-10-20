import re
from email.utils import parsedate_to_datetime

from rootline.catalog import Catalog
from rootline.core.models import DocFacts, ParsedDoc

DOC_ID = re.compile(r"\b(?:SOP|RCA|REF)-[A-Z0-9]+-\d{3}\b")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
LAST_CHECKED = re.compile(
    r"(?i)last (?:checked|updated|modified|reviewed)(?: on)? (\d{4}-\d{2}-\d{2})"
)
ID_FIELDS = ("Document number", "RCA ID", "id")
SERVICE_FIELDS = ("Services", "Affected services or applications")
DATE_FIELDS = ("Effective date", "Date", "last_modified", "retrieved")
ERROR_PATTERNS = [
    re.compile(
        r"\b(?:[a-z_]\w*\.)*[A-Z][A-Za-z]*(?:Error|Exception|Timeout)\b(?::[^\n\"]{1,100})?"
    ),
    re.compile(
        r"\b(?:OOMKilled|CrashLoopBackOff|ImagePullBackOff|Evicted)\b(?: \(exit code \d+\))?"
    ),
    re.compile(
        r"\b[45]\d\d (?:Unauthorized|Forbidden|Not Found|Bad Gateway|Service Unavailable"
        r"|Gateway Timeout|Internal Server Error)\b"
    ),
    re.compile(
        r"(?i)\b(?:no space left on device|too many clients(?: already)?|connection refused"
        r"|permission denied|api key expired)\b"
    ),
]
# A service needs this much evidence. Specific names weigh more than single common words
# such as "orders", which shows up in almost every document here.
SERVICE_MIN_SCORE = 3
MAX_SIGNATURES = 10


def extract_facts(doc: ParsedDoc, catalog: Catalog) -> DocFacts:
    doc_id = declared_id(doc)
    supersedes = str(doc.fields.get("Supersedes", ""))
    return DocFacts(
        doc_id=doc_id,
        title=doc.title,
        services=find_services(doc, catalog),
        alerts=[alert for alert in catalog.alerts if alert in doc.text],
        error_signatures=error_signatures(doc.text),
        effective_date=effective_date(doc),
        mentions=sorted(set(DOC_ID.findall(doc.text)) - {doc_id}),
        supersedes=supersedes if DOC_ID.fullmatch(supersedes) else None,
    )


def declared_id(doc: ParsedDoc) -> str | None:
    for key in ID_FIELDS:
        value = str(doc.fields.get(key, ""))
        if DOC_ID.fullmatch(value):
            return value
    return None


def find_services(doc: ParsedDoc, catalog: Catalog) -> list[str]:
    for key in SERVICE_FIELDS:
        listed = [name.strip() for name in str(doc.fields.get(key, "")).split(",")]
        known = [name for name in listed if name in catalog.services]
        if known:
            return known
    terms = [(name, name, 3) for name in catalog.services]
    terms += [
        (alias, name, alias_weight(alias))
        for name, aliases in catalog.services.items()
        for alias in aliases
    ]
    # Longest terms first, and each match is blanked out, so "orders topic" counts once for
    # order-queue and not again as "orders" for orders-api.
    text = f"{doc.title}\n{doc.text}"
    scores = dict.fromkeys(catalog.services, 0)
    for term, name, weight in sorted(terms, key=lambda t: len(t[0]), reverse=True):
        pattern = re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", re.IGNORECASE)
        text, count = pattern.subn(lambda match: " " * len(match.group()), text)
        scores[name] += weight * count
    return [name for name, score in scores.items() if score >= SERVICE_MIN_SCORE]


def alias_weight(alias: str) -> int:
    return 2 if " " in alias or "-" in alias else 1


def error_signatures(text: str) -> list[str]:
    found: dict[str, str] = {}
    for pattern in ERROR_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group().strip().rstrip(".,;")
            found.setdefault(value.lower(), value)
    return list(found.values())[:MAX_SIGNATURES]


def effective_date(doc: ParsedDoc) -> str | None:
    for key in DATE_FIELDS:
        if match := ISO_DATE.search(str(doc.fields.get(key, ""))):
            return match.group()
    if doc.fields.get("date"):
        return parsedate_to_datetime(str(doc.fields["date"])).date().isoformat()
    if match := LAST_CHECKED.search(doc.text):
        return match.group(1)
    # An undated incident note is as current as the latest date it mentions.
    dates = ISO_DATE.findall(doc.text)
    return max(dates) if dates else None
