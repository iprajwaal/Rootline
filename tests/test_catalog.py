from pathlib import Path
from typing import Any

import yaml

CATALOG_PATH = Path(__file__).parents[1] / "corpus" / "catalog.yaml"


def load_catalog() -> dict[str, Any]:
    catalog: dict[str, Any] = yaml.safe_load(CATALOG_PATH.read_text())
    return catalog


def test_every_fault_uses_known_service_check_and_action() -> None:
    catalog = load_catalog()

    for name, fault in catalog["faults"].items():
        assert fault["service"] in catalog["services"], name
        assert fault["reveal_check"] in catalog["checks"], name
        assert fault["fix_action"] in catalog["actions"], name


def test_service_dependencies_are_known_services() -> None:
    services = load_catalog()["services"]

    for name, service in services.items():
        assert set(service["depends_on"]) <= set(services), name
