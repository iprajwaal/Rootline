import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from bs4 import BeautifulSoup

from rootline.core.models import ParsedDoc
from rootline.ingest.document_builder import DocBuilder
from rootline.ingest.parsers.text_doc import add_text

REPLY_PREFIX = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)
HEADERS = ("from", "to", "date", "subject")


def parse(data: bytes, source_key: str) -> ParsedDoc:
    message = BytesParser(policy=policy.default).parsebytes(data)
    if not isinstance(message, EmailMessage):
        raise ValueError(f"{source_key} is not a MIME email")
    fields = {name: str(message[name]) for name in HEADERS if message[name]}

    builder = DocBuilder()
    title = REPLY_PREFIX.sub("", fields.get("subject", "")).strip()
    builder.add("heading", title, level=1)
    add_text(builder, unquote(body_text(message)))
    return builder.build(source_key, "eml", fields, title=title)


def body_text(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if not isinstance(body, EmailMessage):
        return ""
    content = str(body.get_content())
    if body.get_content_type() == "text/html":
        return BeautifulSoup(content, "html.parser").get_text("\n")
    return content


def unquote(text: str) -> str:
    # Quoted replies keep useful history (the alert, the first report), so keep the text
    # and drop only the "> " markers.
    return "\n".join(
        line.lstrip(">").lstrip() if line.startswith(">") else line for line in text.splitlines()
    )
