from pathlib import Path
from typing import Any

import pytest
import yaml

from rootline.core.models import DocMeta, RcaRecord, SopRecord

CORPUS = Path(__file__).parents[1] / "corpus"
CATALOG: dict[str, Any] = yaml.safe_load((CORPUS / "catalog.yaml").read_text())
SOURCES = sorted((CORPUS / "sources" / "greenfield").glob("*.yaml"))
SOP_SOURCES = [path for path in SOURCES if path.name.startswith("sop-")]
RCA_SOURCES = [path for path in SOURCES if path.name.startswith("rca-")]
CATALOG_ALERTS = {fault["alert"] for fault in CATALOG["faults"].values()}
SEVERITY_LEVELS = {"Critical", "High", "Medium", "Low"}


def load(path: Path) -> dict[str, Any]:
    source: dict[str, Any] = yaml.safe_load(path.read_text())
    return source


def meta_of(path: Path) -> DocMeta:
    return DocMeta.model_validate(load(path)["meta"])


def sop_of(path: Path) -> SopRecord:
    return SopRecord.model_validate(load(path)["record"])


def rca_of(path: Path) -> RcaRecord:
    return RcaRecord.model_validate(load(path)["record"])


def test_there_are_six_sops_and_six_rcas() -> None:
    assert (len(SOP_SOURCES), len(RCA_SOURCES)) == (6, 6)


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_render_format_is_supported(path: Path) -> None:
    assert load(path)["render_as"] in {"docx", "pdf", "md"}


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_doc_id_and_type_match_file_name(path: Path) -> None:
    meta = meta_of(path)

    assert meta.doc_id.lower() == path.stem
    assert path.stem.startswith(meta.doc_type)


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_services_are_in_catalog(path: Path) -> None:
    assert set(meta_of(path).services) <= set(CATALOG["services"])


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_links_point_to_existing_docs(path: Path) -> None:
    meta = meta_of(path)
    known = {meta_of(source).doc_id for source in SOURCES}
    links = set(meta.related) | ({meta.supersedes} if meta.supersedes else set())

    assert links <= known


@pytest.mark.parametrize("path", SOP_SOURCES, ids=lambda path: path.name)
def test_sop_alerts_are_in_catalog(path: Path) -> None:
    assert set(sop_of(path).alerts) <= CATALOG_ALERTS


@pytest.mark.parametrize("path", SOP_SOURCES, ids=lambda path: path.name)
def test_sop_step_commands_match_catalog(path: Path) -> None:
    for step in sop_of(path).steps:
        if step.action:
            assert step.command == CATALOG["actions"][step.action], step.ordinal
        if step.check_name:
            assert step.command == CATALOG["checks"][step.check_name], step.ordinal


@pytest.mark.parametrize("path", RCA_SOURCES, ids=lambda path: path.name)
def test_rca_severity_uses_template_levels(path: Path) -> None:
    assert rca_of(path).severity in SEVERITY_LEVELS


@pytest.mark.parametrize("path", RCA_SOURCES, ids=lambda path: path.name)
def test_rca_has_five_whys_and_owned_action_items(path: Path) -> None:
    rca = rca_of(path)

    assert len(rca.five_whys) == 5
    assert rca.action_items
    assert all(item.owner and item.due_date for item in rca.action_items)
