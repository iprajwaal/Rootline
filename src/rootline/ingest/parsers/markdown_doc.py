from datetime import date, datetime
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

from rootline.ingest.document_builder import DocBuilder
from rootline.models import ParsedDoc

FRONT_MATTER = "---\n"


def parse(data: bytes, source_key: str) -> ParsedDoc:
    fields, body = split_front_matter(data.decode("utf-8", errors="replace"))
    builder = DocBuilder()
    add_tokens(builder, MarkdownIt("commonmark").enable("table").parse(body))
    return builder.build(source_key, "md", fields)


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(FRONT_MATTER) or text.count(FRONT_MATTER) < 2:
        return {}, text
    _, header, body = text.split(FRONT_MATTER, 2)
    loaded = yaml.safe_load(header)
    if not isinstance(loaded, dict):
        return {}, text
    return {str(key): plain(value) for key, value in loaded.items()}, body


def plain(value: Any) -> Any:
    # Fields end up in a JSON column, and YAML turns unquoted dates into date objects.
    return value.isoformat() if isinstance(value, date | datetime) else value


def add_tokens(builder: DocBuilder, tokens: list[Token]) -> None:
    ordered: list[bool] = []
    rows: list[list[str]] = []
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        match token.type:
            case "heading_open" if following:
                builder.add("heading", inline_text(following), level=int(token.tag[1]))
            case "bullet_list_open" | "ordered_list_open":
                ordered.append(token.type == "ordered_list_open")
            case "bullet_list_close" | "ordered_list_close":
                ordered.pop()
            case "list_item_open" if following and following.type == "paragraph_open":
                text = inline_text(tokens[index + 2])
                builder.add("list_item", f"{token.info}. {text}" if ordered[-1] else text)
            case "paragraph_open" if following and not ordered:
                builder.add("paragraph", inline_text(following))
            case "fence" | "code_block":
                builder.add("code", token.content)
            case "tr_open":
                rows.append([])
            case "inline" if (
                rows and token.level > 0 and tokens[index - 1].type in ("th_open", "td_open")
            ):
                rows[-1].append(inline_text(token))
            case "table_close":
                builder.add_table(rows)
                rows = []


def inline_text(token: Token) -> str:
    parts = []
    for child in token.children or []:
        if child.type in ("text", "code_inline", "html_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append("\n")
    return "".join(parts)
