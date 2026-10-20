import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Catalog:
    services: dict[str, list[str]]
    alerts: dict[str, str]
    checks: dict[str, str]
    actions: dict[str, str]


def load_catalog(path: Path) -> Catalog:
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    return Catalog(
        services={name: service["aliases"] for name, service in raw["services"].items()},
        alerts={fault["alert"]: fault["service"] for fault in raw["faults"].values()},
        checks=raw["checks"],
        actions=raw["actions"],
    )


def named_services(text: str, services: dict[str, list[str]]) -> set[str]:
    lowered = text.lower()
    return {
        name
        for name, aliases in services.items()
        if any(
            re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", lowered)
            for term in [name, *aliases]
        )
    }
