import re
import unicodedata
from typing import Any

from rootline.core.models import Block, BlockKind, ParsedDoc

ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
SPACES = re.compile(r"[ \t]+")
DOT_LEADER = re.compile(r"\.{5,}\s*\d+$")
TOC_TITLES = {"table of contents", "contents"}
HEADER_ROW = ("field", "value")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = ZERO_WIDTH.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(SPACES.sub(" ", line).strip() for line in text.split("\n")).strip()


class DocBuilder:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.length = 0
        self.in_toc = False

    def add(self, kind: BlockKind, text: str, level: int = 0) -> None:
        self.append(kind, normalize(text), level, [])

    def add_table(self, rows: list[list[str]]) -> None:
        cleaned = [[normalize(cell).replace("\n", " ") for cell in row] for row in rows]
        cleaned = [row for row in cleaned if any(row)]
        self.append("table", "\n".join(" | ".join(row) for row in cleaned), 0, cleaned)

    def append(self, kind: BlockKind, text: str, level: int, rows: list[list[str]]) -> None:
        if not text or DOT_LEADER.search(text):
            return
        # A table of contents only repeats the headings, so skip it up to the next heading.
        if kind == "heading":
            self.in_toc = text.lower() in TOC_TITLES
            if self.in_toc:
                return
        elif self.in_toc:
            return
        start = self.length + 2 if self.blocks else 0
        end = start + len(text)
        self.blocks.append(
            Block(kind=kind, text=text, level=level, start=start, end=end, rows=rows)
        )
        self.length = end

    def build(
        self, source_key: str, source_format: str, fields: dict[str, Any], title: str = ""
    ) -> ParsedDoc:
        fields = label_fields(self.blocks) | fields
        return ParsedDoc(
            source_key=source_key,
            source_format=source_format,
            title=title or pick_title(fields, self.blocks),
            fields=fields,
            blocks=self.blocks,
            text="\n\n".join(block.text for block in self.blocks),
        )


def label_fields(blocks: list[Block]) -> dict[str, str]:
    # Two-column tables like a document control table or an RCA summary hold label/value pairs.
    fields = {}
    for block in blocks:
        if block.kind != "table" or any(len(row) != 2 for row in block.rows):
            continue
        for label, value in block.rows:
            if (label.lower(), value.lower()) != HEADER_ROW and label and label not in fields:
                fields[label] = value
    return fields


def pick_title(fields: dict[str, Any], blocks: list[Block]) -> str:
    for key in ("title", "Document title"):
        if fields.get(key):
            return str(fields[key])
    for block in blocks:
        if block.kind == "heading" and block.level == 1:
            return block.text
    if fields.get("core_title"):
        return str(fields["core_title"])
    return blocks[0].text.split("\n")[0][:120] if blocks else ""
