import email
import json
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import docx
import pytest
import yaml
from pypdf import PdfReader

from rootline.core.models import SopRecord
from rootline.corpus.brownfield import generate
from rootline.corpus.layout import load_document

CORPUS = Path(__file__).parents[1] / "corpus"
OUTPUT_DIR = CORPUS / "brownfield"
TRUTH_PATH = CORPUS / "ground_truth" / "brownfield.jsonl"
CATALOG: dict[str, Any] = yaml.safe_load((CORPUS / "catalog.yaml").read_text())
BROWNFIELD_SOURCES = sorted((CORPUS / "sources" / "brownfield").glob("*.yaml"))


def truth() -> list[dict[str, Any]]:
    return [json.loads(line) for line in TRUTH_PATH.read_text().splitlines()]


def file_text(path: Path) -> str:
    match path.suffix:
        case ".docx":
            return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        case ".pdf":
            return "\n".join(page.extract_text() for page in PdfReader(path).pages)
        case ".eml":
            message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
            assert isinstance(message, EmailMessage)
            return str(message.get_content())
        case _:
            return path.read_text()


def test_ground_truth_lists_every_brownfield_file() -> None:
    assert {row["file"] for row in truth()} == {
        f"brownfield/{path.name}" for path in OUTPUT_DIR.iterdir()
    }


def test_generation_is_reproducible(tmp_path: Path) -> None:
    generate(CORPUS, tmp_path / "brownfield", tmp_path / "truth.jsonl")

    for path in OUTPUT_DIR.iterdir():
        assert (tmp_path / "brownfield" / path.name).read_bytes() == path.read_bytes(), path.name
    assert (tmp_path / "truth.jsonl").read_text() == TRUTH_PATH.read_text()


@pytest.mark.parametrize("row", [r for r in truth() if r["canaries"]], ids=lambda r: r["file"])
def test_every_canary_is_planted_in_its_file(row: dict[str, Any]) -> None:
    text = file_text(CORPUS / row["file"])

    for value in row["canaries"]:
        assert value in text


@pytest.mark.parametrize("row", [r for r in truth() if r["outdated"]], ids=lambda r: r["file"])
def test_outdated_files_carry_their_stale_values(row: dict[str, Any]) -> None:
    text = file_text(CORPUS / row["file"])

    for value in row["stale_values"]:
        assert value in text


def test_mixed_docs_pair_an_rca_with_an_sop() -> None:
    mixed = [row for row in truth() if row["doc_type"] == "mixed"]

    assert mixed
    for row in mixed:
        assert {doc_id.split("-")[0] for doc_id in row["source_ids"]} == {"RCA", "SOP"}


def test_same_bytes_as_points_to_an_identical_file() -> None:
    copies = [row for row in truth() if row["same_bytes_as"]]

    assert len(copies) >= 2
    for row in copies:
        original = CORPUS / row["same_bytes_as"]
        assert (CORPUS / row["file"]).read_bytes() == original.read_bytes(), row["file"]


@pytest.mark.parametrize("path", BROWNFIELD_SOURCES, ids=lambda path: path.name)
def test_brownfield_sop_commands_match_catalog(path: Path) -> None:
    _, record = load_document(path)
    if not isinstance(record, SopRecord):
        return

    for step in record.steps:
        if step.action:
            assert step.command == CATALOG["actions"][step.action], step.ordinal
        if step.check_name:
            assert step.command == CATALOG["checks"][step.check_name], step.ordinal
