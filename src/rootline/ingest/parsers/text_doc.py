import re

from rootline.ingest.document_builder import DocBuilder
from rootline.models import ParsedDoc

LIST_MARKER = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+)")
BULLET = re.compile(r"^[-*•]\s+")
BLANK_LINES = re.compile(r"\n\s*\n")


def parse(data: bytes, source_key: str) -> ParsedDoc:
    builder = DocBuilder()
    add_text(builder, data.decode("utf-8", errors="replace"), first_line_is_title=True)
    return builder.build(source_key, "txt", {})


def add_text(
    builder: DocBuilder,
    content: str,
    first_line_is_title: bool = False,
    infer_headings: bool = True,
) -> None:
    # Guessing headings from short lines only makes sense when the format has no real
    # headings of its own (plain text, email bodies, unstyled Word or PDF files).
    chunks = [chunk.strip("\n") for chunk in BLANK_LINES.split(content) if chunk.strip()]
    for index, chunk in enumerate(chunks):
        lines = chunk.split("\n")
        if first_line_is_title and index == 0:
            builder.add("heading", lines.pop(0), level=1)
        if infer_headings and lines and is_heading(lines[0]) and starts_section(lines):
            builder.add("heading", lines.pop(0).strip().rstrip(":"), level=2)
        add_lines(builder, lines)


def starts_section(lines: list[str]) -> bool:
    return len(lines) == 1 or lines[0].rstrip().endswith(":") or is_list_item(lines[1])


def add_lines(builder: DocBuilder, lines: list[str]) -> None:
    paragraph: list[str] = []
    item: list[str] = []
    for line in lines:
        if is_list_item(line):
            builder.add("paragraph", "\n".join(paragraph))
            builder.add("list_item", "\n".join(item))
            paragraph, item = [], [BULLET.sub("", line.strip())]
        elif item:
            # Inside one chunk, a line after a list item continues that item (wrapped text,
            # an indented command, an "expected" line).
            item.append(line)
        else:
            paragraph.append(line)
    builder.add("paragraph", "\n".join(paragraph))
    builder.add("list_item", "\n".join(item))


def is_list_item(line: str) -> bool:
    return bool(LIST_MARKER.match(line.strip()))


def is_heading(line: str) -> bool:
    text = line.strip()
    return (
        0 < len(text) <= 40
        and len(text.split()) <= 6
        and any(char.isalpha() for char in text)
        and not is_list_item(text)
        and not text.endswith((".", ",", ";"))
        and not text.startswith(("(", ">"))
        and ":" not in text.rstrip(":")
    )
