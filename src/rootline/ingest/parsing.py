from collections.abc import Callable
from pathlib import Path, PurePosixPath

from rootline.ingest.parsers import (
    docx_doc,
    email_doc,
    html_doc,
    markdown_doc,
    pdf_doc,
    slack_doc,
    text_doc,
)
from rootline.models import ParsedDoc

ParseFn = Callable[[bytes, str], ParsedDoc]

PARSERS: dict[str, ParseFn] = {
    ".md": markdown_doc.parse,
    ".markdown": markdown_doc.parse,
    ".txt": text_doc.parse,
    ".html": html_doc.parse,
    ".htm": html_doc.parse,
    ".eml": email_doc.parse,
    ".json": slack_doc.parse,
    ".docx": docx_doc.parse,
    ".pdf": pdf_doc.parse,
}


class UnsupportedFormat(ValueError):
    pass


def parse_bytes(data: bytes, source_key: str) -> ParsedDoc:
    suffix = PurePosixPath(source_key).suffix.lower()
    parser = PARSERS.get(suffix)
    if parser is None:
        raise UnsupportedFormat(
            f"no parser for {suffix or 'a file without extension'}: {source_key}"
        )
    return parser(data, source_key)


def parse_file(path: Path) -> ParsedDoc:
    return parse_bytes(path.read_bytes(), path.as_posix())
