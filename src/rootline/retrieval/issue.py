import re

from pydantic import BaseModel

# Longer input is almost always a pasted log; the first part holds the error.
MAX_CHARS = 20_000
QUERY_CHARS = 600

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FRAME = re.compile(
    r'^\s*(?:File ".+", line \d+'
    r"|at [\w$.<>/\\-]+ ?\(.*\)$"
    r"|at \S+:\d+:\d+\)?$"
    r"|\.\.\. \d+ more$"
    r"|Traceback \(most recent call last\):$"
    r"|goroutine \d+ \[.*\]:$"
    r"|/\S+\.go:\d+"
    r"|[\^~ ]+$)"
)
PYTHON_FRAME = re.compile(r'^\s*File ".+", line \d+')
# The level sits near the start of a log line, after the timestamp.
QUIET_LEVEL = re.compile(r"^.{0,40}?\b(?:TRACE|DEBUG|INFO|NOTICE)\b")
# Order matters: key=value IDs go first, whole timestamps before bare dates and times.
NOISE = [
    re.compile(r"(?i)\b(?:pid|tid|thread|req|request_id|trace_id|span_id)[=:]\S+"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} \d{2}:\d{2}:\d{2}"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),
    re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),
    re.compile(r"\b(?:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|FATAL|CRITICAL)\b"),
]
# Kubernetes pod names end in a ReplicaSet hash and a random suffix: orders-api-7d9f8b6c5-x2klm.
POD = re.compile(
    r"\b([a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*)-(?=[a-z0-9]*\d)[a-z0-9]{6,10}-[a-z0-9]{5}\b"
)
HEX = re.compile(r"(?i)\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")
# Short numbers such as exit code 137 or status 503 carry meaning; long ones are IDs and ports.
LONG_NUMBER = re.compile(r"\b\d{4,}\b")
EMPTY_BRACKETS = re.compile(r"\[\s*\]|\(\s*\)|\{\s*\}")
# Log lines often start with the source in brackets: [orders-api] or [main].
SOURCE_TAG = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
SPACES = re.compile(r"\s+")


class Issue(BaseModel):
    alerts: list[str]
    undocumented_alerts: list[str]
    services: list[str]
    errors: list[str]
    text: str
    query: str


def clean(text: str) -> list[str]:
    lines = ANSI.sub("", text[:MAX_CHARS]).splitlines()
    kept: list[str] = []
    after_python_frame = False
    for line in lines:
        # The source line under a Python frame is code, not part of the error.
        if FRAME.match(line) or (after_python_frame and line[:1].isspace()):
            after_python_frame = bool(PYTHON_FRAME.match(line))
            continue
        after_python_frame = False
        kept.append(line)
    loud = [line for line in kept if not QUIET_LEVEL.match(line)]
    return list(dict.fromkeys(filter(None, map(scrub, loud or kept))))


def scrub(line: str) -> str:
    for pattern in NOISE:
        line = pattern.sub(" ", line)
    line = POD.sub(r"\1", line)
    line = LONG_NUMBER.sub(" ", HEX.sub(" ", line))
    return SPACES.sub(" ", EMPTY_BRACKETS.sub(" ", line)).strip(" -:|")


def error_lines(lines: list[str], errors: list[str]) -> list[str]:
    # With a known error in the text, one line per error is the query. The other lines of a
    # log are mostly about the same service and pull in its unrelated documents.
    if not errors:
        return lines
    picked = [next(line for line in lines if error.lower() in line.lower()) for error in errors]
    return list(dict.fromkeys(picked))


def compose_query(parts: list[str], drop: list[str]) -> str:
    for word in drop:
        parts = [part.replace(word, " ") for part in parts]
    parts = [SPACES.sub(" ", SOURCE_TAG.sub("", part)).strip(" -:|") for part in parts]
    return " ".join(dict.fromkeys(filter(None, parts)))[:QUERY_CHARS]
