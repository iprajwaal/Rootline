from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from rootline.agent.executor import Executor, SandboxUnavailable
from rootline.agent.knowledge import Knowledge
from rootline.catalog import Catalog

ACTION_TOOL = "run_action"
RESULT_CHARS = 1500


class NoArgs(BaseModel):
    pass


class CheckArgs(BaseModel):
    name: str = Field(description="a check name from the list in the instructions")


class SearchArgs(BaseModel):
    query: str = Field(description="an error message, symptom or service")


class SopArgs(BaseModel):
    doc_id: str = Field(description="an SOP ID such as SOP-DB-001")


class ActionArgs(BaseModel):
    name: str = Field(description="an action name from the list in the instructions")
    reason: str = Field(description="why this action fixes the problem, based on the checks")


@dataclass(frozen=True)
class Toolbox:
    executor: Executor
    knowledge: Knowledge
    catalog: Catalog

    def specs(self) -> list[StructuredTool]:
        # The model only sees these schemas. The graph runs each call itself, so an action
        # can wait for a person's approval before anything changes.
        return [
            spec("check_health", "Health of each part of the system.", NoArgs),
            spec("run_check", "Run one read-only diagnostic check and get its output.", CheckArgs),
            spec("search_docs", "Search the internal RCAs and SOPs.", SearchArgs),
            spec("read_sop", "Read the steps of an internal SOP word for word.", SopArgs),
            spec("search_references", "Search public documentation pages.", SearchArgs),
            spec(ACTION_TOOL, "Run one fixing action. A person approves it first.", ActionArgs),
        ]

    def run(self, name: str, args: dict[str, Any]) -> str:
        tools: dict[str, Callable[[dict[str, Any]], str]] = {
            "check_health": lambda _: self.health(),
            "run_check": lambda a: self.check(str(a.get("name", ""))),
            "search_docs": lambda a: self.search(str(a.get("query", ""))),
            "read_sop": lambda a: self.read_sop(str(a.get("doc_id", ""))),
            "search_references": lambda a: self.references(str(a.get("query", ""))),
        }
        if name not in tools:
            return f"unknown tool {name}"
        try:
            return tools[name](args)[:RESULT_CHARS]
        except SandboxUnavailable as error:
            return f"the sandbox is not reachable: {error}"

    def health(self) -> str:
        health = self.executor.sandbox.health()
        failing = [part for part, state in health["components"].items() if state != "ok"]
        return f"status {health['status']}; failing: {', '.join(failing) or 'nothing'}"

    def check(self, name: str) -> str:
        if name not in self.catalog.checks:
            return f"unknown check {name}; known checks: {', '.join(self.catalog.checks)}"
        return self.executor.check(name).output or "(no output)"

    def search(self, query: str) -> str:
        found = self.knowledge.search(query)
        lines = [
            f"{hit.doc_id} v{hit.version} [{hit.doc_type.upper()}] {hit.title}"
            for hit in [*found.sop.hits, *found.rca.hits]
        ]
        return "\n".join(lines) or "no internal SOP or RCA matches"

    def read_sop(self, doc_id: str) -> str:
        doc = self.knowledge.document(doc_id.upper())
        if doc is None or doc.doc_type != "sop":
            return f"{doc_id} is not an SOP in the library"
        lines = [f"{doc.doc_id} v{doc.version} {doc.title}"]
        for step in doc.steps:
            parts = [f"{step.ordinal}. {step.instruction}"]
            parts += [f"command: {step.command}"] if step.command else []
            parts += [f"action: {step.action}"] if step.action else []
            parts += [f"expect: {step.expect}"] if step.expect else []
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    def references(self, query: str) -> str:
        pages = self.knowledge.references(query)
        return "\n".join(f"{p.doc_id} {p.title} ({p.publisher})" for p in pages) or "no pages"


def spec(name: str, description: str, schema: type[BaseModel]) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda **kwargs: "", name=name, description=description, args_schema=schema
    )
