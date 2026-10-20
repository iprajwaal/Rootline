import re
from typing import Any

# Control characters carry no meaning in a document or a query, and Postgres text cannot
# hold NUL at all, so text from outside is stripped of them before it is stored or searched.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def printable(text: str) -> str:
    return CONTROL.sub("", text)


def printable_all(value: Any) -> Any:
    # Walks a dumped model, so every string field of a parsed file is cleaned the same way.
    if isinstance(value, str):
        return printable(value)
    if isinstance(value, list):
        return [printable_all(item) for item in value]
    if isinstance(value, dict):
        return {key: printable_all(item) for key, item in value.items()}
    return value
