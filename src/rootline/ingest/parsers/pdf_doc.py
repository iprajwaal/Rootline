import io
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pdfplumber
from pdfplumber.page import Page

from rootline.core.models import ParsedDoc
from rootline.ingest.document_builder import DocBuilder
from rootline.ingest.parsers.text_doc import add_text

PAGE_NUMBER = re.compile(r"\bpage \d+( of \d+)?\b", re.IGNORECASE)
DIGITS = re.compile(r"\d+")
# pdfminer writes glyphs it cannot map as "(cid:N)". At the start of a line it is almost
# always a bullet.
LEADING_CID = re.compile(r"^\(cid:\d+\)\s*")
CID = re.compile(r"\(cid:\d+\)")
# Lines this close to the top or bottom edge are where running headers and footers live.
MARGIN_SHARE = 0.12
DEFAULT_BODY_SIZE = 10.0


@dataclass(frozen=True)
class Line:
    text: str
    top: float
    size: float
    bold: bool
    in_margin: bool


@dataclass(frozen=True)
class TableItem:
    top: float
    rows: list[list[str]]


Item = Line | TableItem


def parse(data: bytes, source_key: str) -> ParsedDoc:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [page_items(page) for page in pdf.pages]
        info = pdf.metadata
    repeated = repeated_lines(pages) if len(pages) > 1 else set()
    body = [i for page in pages for i in page if isinstance(i, Line) and not dropped(i, repeated)]
    sizes = Counter(round(line.size) for line in body)
    body_size = float(sizes.most_common(1)[0][0]) if sizes else DEFAULT_BODY_SIZE
    structured = any(heading_level(line, body_size) for line in body)

    builder = DocBuilder()
    for items in pages:
        kept = [i for i in items if not dropped(i, repeated)]
        add_page(builder, kept, body_size, infer_headings=not structured)
    fields = {"core_title": str(info.get("Title", "")), "core_author": str(info.get("Author", ""))}
    return builder.build(source_key, "pdf", {k: v for k, v in fields.items() if v})


def page_items(page: Page) -> list[Item]:
    tables = page.find_tables()
    boxes = [table.bbox for table in tables]

    def outside_tables(obj: dict[str, Any]) -> bool:
        return not any(
            x0 <= obj["x0"] and obj["x1"] <= x1 and top <= obj["top"] and obj["bottom"] <= bottom
            for x0, top, x1, bottom in boxes
        )

    margin = page.height * MARGIN_SHARE
    items: list[Item] = [
        Line(
            text=CID.sub("", LEADING_CID.sub("\u2022 ", line["text"])),
            top=line["top"],
            size=max(char["size"] for char in line["chars"]),
            bold=all("Bold" in char["fontname"] for char in line["chars"]),
            in_margin=line["top"] < margin or line["bottom"] > page.height - margin,
        )
        for line in page.filter(outside_tables).extract_text_lines(return_chars=True)
    ]
    items += [
        TableItem(table.bbox[1], [[cell or "" for cell in row] for row in table.extract()])
        for table in tables
    ]
    return sorted(items, key=lambda item: item.top)


def repeated_lines(pages: list[list[Item]]) -> set[str]:
    # Running headers and footers repeat on every page; page numbers differ only in digits.
    seen: Counter[str] = Counter()
    for items in pages:
        seen.update({line_key(i.text) for i in items if isinstance(i, Line) and i.in_margin})
    return {key for key, count in seen.items() if count >= 2}


def dropped(item: Item, repeated: set[str]) -> bool:
    if not isinstance(item, Line):
        return False
    return bool(PAGE_NUMBER.search(item.text)) or (
        item.in_margin and line_key(item.text) in repeated
    )


def line_key(text: str) -> str:
    return DIGITS.sub("#", text.strip().lower())


def add_page(
    builder: DocBuilder, items: list[Item], body_size: float, infer_headings: bool
) -> None:
    text_lines: list[str] = []
    heading: tuple[int, str] | None = None
    previous_top: float | None = None

    def flush() -> None:
        nonlocal heading, text_lines
        if heading:
            builder.add("heading", heading[1], level=heading[0])
        # Line breaks inside a PDF paragraph are only where the page wrapped the text.
        add_text(builder, "\n".join(text_lines), infer_headings=infer_headings, line_joiner=" ")
        heading, text_lines = None, []

    for item in items:
        if isinstance(item, TableItem):
            flush()
            builder.add_table(item.rows)
            previous_top = None
            continue
        level = heading_level(item, body_size)
        gap = item.top - previous_top if previous_top is not None else 0.0
        if level and heading and heading[0] == level and gap <= item.size * 1.5:
            heading = (level, f"{heading[1]} {item.text}")
        elif level:
            flush()
            heading = (level, item.text)
        else:
            if heading:
                builder.add("heading", heading[1], level=heading[0])
                heading = None
            if text_lines and gap > body_size * 1.35:
                text_lines.append("")
            text_lines.append(item.text)
        previous_top = item.top
    flush()


def heading_level(line: Line, body_size: float) -> int:
    if line.size >= body_size * 1.6:
        return 1
    if line.size >= body_size * 1.3:
        return 2
    if line.size >= body_size * 1.1:
        return 3
    if line.bold and len(line.text) <= 80:
        return 4
    return 0
