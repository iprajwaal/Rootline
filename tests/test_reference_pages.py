from pathlib import Path
from typing import Any

import pytest
import yaml

REFERENCE_DIR = Path(__file__).parents[1] / "corpus" / "reference"
PAGES = sorted(REFERENCE_DIR.glob("*.md"))
REQUIRED_FIELDS = {
    "id",
    "type",
    "title",
    "publisher",
    "source_url",
    "licence",
    "attribution",
    "changes",
    "retrieved",
    "topics",
}
# Licences that allow storing an adapted copy with attribution.
ALLOWED_LICENCES = {"CC BY 4.0", "CC BY-SA 4.0", "PostgreSQL License"}


def split_page(path: Path) -> tuple[dict[str, Any], str]:
    _, header, body = path.read_text().split("---\n", 2)
    meta: dict[str, Any] = yaml.safe_load(header)
    return meta, body


def test_there_are_ten_reference_pages() -> None:
    assert len(PAGES) == 10


@pytest.mark.parametrize("path", PAGES, ids=lambda path: path.name)
def test_front_matter_is_complete(path: Path) -> None:
    meta, _ = split_page(path)

    assert set(meta) >= REQUIRED_FIELDS
    assert meta["type"] == "ref"
    assert meta["id"].lower() == path.stem


@pytest.mark.parametrize("path", PAGES, ids=lambda path: path.name)
def test_licence_allows_storing_an_adapted_copy(path: Path) -> None:
    meta, _ = split_page(path)

    assert meta["licence"] in ALLOWED_LICENCES
    assert meta["source_url"].startswith("https://")


@pytest.mark.parametrize("path", PAGES, ids=lambda path: path.name)
def test_page_has_sections(path: Path) -> None:
    _, body = split_page(path)

    assert body.count("\n## ") >= 1
