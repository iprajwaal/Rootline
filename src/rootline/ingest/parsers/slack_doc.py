import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from rootline.core.models import ParsedDoc
from rootline.ingest.document_builder import DocBuilder


def parse(data: bytes, source_key: str) -> ParsedDoc:
    payload = json.loads(data)
    # A channel export file is either {"channel": ..., "messages": [...]} or a bare list.
    messages: list[dict[str, Any]] = payload["messages"] if isinstance(payload, dict) else payload
    channel = payload.get("channel", "") if isinstance(payload, dict) else ""

    builder = DocBuilder()
    title = channel or PurePosixPath(source_key).stem
    builder.add("heading", title, level=1)
    for message in messages:
        when = datetime.fromtimestamp(float(message["ts"]), UTC).strftime("%Y-%m-%d %H:%M")
        who = message.get("user_name") or message.get("user") or "unknown"
        builder.add("paragraph", f"{when} {who}: {message.get('text', '')}")
    return builder.build(
        source_key, "json", {"channel": channel, "messages": len(messages)}, title=title
    )
