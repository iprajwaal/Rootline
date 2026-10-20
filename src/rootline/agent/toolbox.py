from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from rootline.agent.executor import Executor, SandboxUnavailable
from rootline.agent.knowledge import Knowledge
from rootline.catalog import Catalog
from rootline.retrieval.documents import as_text

TRY_TOOL = "try_fix"
RESULT_CHARS = 1500
REFERENCE_PASSAGES = 2


class NoArgs(BaseModel):
    pass


class CheckArgs(BaseModel):
    name: str = Field(description="a check name from the list in the instructions")


class SearchArgs(BaseModel):
    query: str = Field(description="an error message, symptom or service")


class SopArgs(BaseModel):
    doc_id: str = Field(description="an SOP ID such as SOP-DB-001")


class FixArgs(BaseModel):
    name: str = Field(description="an action name from the list in the instructions")


def spec(name: str, description: str, schema: type[BaseModel]) -> BaseTool:
    # The model only sees the schema; the graph runs each call itself.
    return StructuredTool.from_function(
        func=lambda **kwargs: "", name=name, description=description, args_schema=schema
    )


TOOLS = [
    spec("check_health", "Health of each part of the system.", NoArgs),
    spec("run_check", "Run one read-only diagnostic check and get its output.", CheckArgs),
    spec("search_docs", "Search the internal RCAs and SOPs.", SearchArgs),
    spec("read_sop", "Read the steps of an internal SOP word for word.", SopArgs),
    spec(
        "search_references", "Search public documentation and read the closest parts.", SearchArgs
    ),
    spec(
        TRY_TOOL,
        "Try one fixing action on a copy of the system and see what it would fix."
        " Nothing real changes.",
        FixArgs,
    ),
]


@dataclass(frozen=True)
class Toolbox:
    executor: Executor
    knowledge: Knowledge

    @property
    def catalog(self) -> Catalog:
        return self.knowledge.catalog

    def run(self, name: str, args: dict[str, Any]) -> str:
        tools: dict[str, Callable[[str], str]] = {
            "check_health": lambda _: self.health(),
            "run_check": self.check,
            "search_docs": self.search,
            "read_sop": self.read_sop,
            "search_references": self.references,
            TRY_TOOL: self.try_fix,
        }
        if name not in tools:
            return f"unknown tool {name}"
        try:
            return tools[name](target(args))[:RESULT_CHARS]
        except SandboxUnavailable as error:
            return f"the sandbox is not reachable: {error}"

    def command(self, name: str, args: dict[str, Any]) -> str:
        # The shell command a call stands for, shown to the engineer and kept for drafting.
        if name == "check_health":
            return self.catalog.checks.get("health", "")
        if name == "run_check":
            return self.catalog.checks.get(target(args), "")
        if name == TRY_TOOL:
            return self.catalog.actions.get(target(args), "")
        return ""

    def health(self) -> str:
        health = self.executor.sandbox.health()
        failing = [part for part, state in health["components"].items() if state != "ok"]
        return f"status {health['status']}; failing: {', '.join(failing) or 'nothing'}"

    def check(self, name: str) -> str:
        if name not in self.catalog.checks:
            return f"unknown check {name}; known checks: {', '.join(self.catalog.checks)}"
        return self.executor.check(name).output or "(no output)"

    def try_fix(self, name: str) -> str:
        if name not in self.catalog.actions:
            return f"unknown action {name}; known actions: {', '.join(self.catalog.actions)}"
        result = self.executor.dry_run(name)
        return f"{result.output}; {result.note}"

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
        return as_text(doc)

    def references(self, query: str) -> str:
        pages = self.knowledge.references(query)
        found = self.knowledge.passages(
            query, [page.doc_id for page in pages], "ref", REFERENCE_PASSAGES
        )
        lines = [f"{page.doc_id} {page.title} ({page.publisher})" for page in pages]
        lines += [f"[{p.doc_id} · {p.section}]\n{p.text}" for p in found]
        return "\n".join(lines) or "no public pages match"


def target(args: dict[str, Any]) -> str:
    # Every tool takes at most one argument that matters for running it.
    for key in ("name", "query", "doc_id"):
        if args.get(key):
            return str(args[key]).strip()
    return ""
