import re

from bs4 import BeautifulSoup, Tag

from rootline.ingest.document_builder import DocBuilder
from rootline.models import ParsedDoc

BOILERPLATE_TAGS = ["script", "style", "nav", "header", "footer"]
BOILERPLATE_IDS = ["header", "footer", "breadcrumbs", "navigation", "sidebar"]
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "pre"]
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse(data: bytes, source_key: str) -> ParsedDoc:
    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(BOILERPLATE_TAGS):
        tag.decompose()
    for element_id in BOILERPLATE_IDS:
        for tag in soup.find_all(id=element_id):
            tag.decompose()

    fields = {}
    metadata = soup.find(class_="page-metadata")
    if isinstance(metadata, Tag):
        text = metadata.get_text(" ", strip=True)
        fields["page_metadata"] = text
        if match := DATE.search(text):
            fields["last_modified"] = match.group()
        metadata.decompose()

    builder = DocBuilder()
    title = page_title(soup)
    builder.add("heading", title, level=1)
    root = soup.find(id="main-content")
    add_elements(builder, root if isinstance(root, Tag) else soup)
    return builder.build(source_key, "html", fields, title=title)


def page_title(soup: BeautifulSoup) -> str:
    heading = soup.find(id="title-heading") or soup.find("h1")
    if isinstance(heading, Tag):
        title = heading.get_text(" ", strip=True)
        heading.decompose()
        return title
    # Wiki exports often title pages as "Space : Page title".
    return soup.title.get_text(strip=True).split(" : ")[-1] if soup.title else ""


def add_elements(builder: DocBuilder, root: Tag) -> None:
    for element in root.find_all(BLOCK_TAGS):
        if element.name != "table" and element.find_parent("table"):
            continue
        if element.name == "p" and element.find_parent("li"):
            continue
        if element.name == "table":
            rows = [
                [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                for row in element.find_all("tr")
            ]
            builder.add_table(rows)
        elif element.name == "pre":
            builder.add("code", element.get_text())
        elif element.name == "li":
            builder.add("list_item", element.get_text(" ", strip=True))
        elif element.name.startswith("h"):
            builder.add("heading", element.get_text(" ", strip=True), level=int(element.name[1]))
        else:
            builder.add("paragraph", element.get_text(" ", strip=True))
