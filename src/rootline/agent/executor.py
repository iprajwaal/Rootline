from dataclasses import dataclass
from typing import Any, Literal

import httpx
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from rootline.core.models import SopStep
from rootline.ingest.redaction import redact

Section = Literal["steps", "rollback"]
STEP = """
    select ordinal, instruction, command, action, check_name, expect from sop_steps
    where doc_id = %s and version = %s and section = %s and ordinal = %s
"""


class SandboxUnavailable(RuntimeError):
    pass


class StepResult(BaseModel):
    kind: Literal["action", "check"]
    name: str
    output: str
    # For an action tried on a copy: True or False when it fixed something, None when it
    # fixed nothing. Always None for a read-only check.
    met: bool | None
    note: str
    fixed: list[str] = []


@dataclass(frozen=True)
class ShopLite:
    client: httpx.Client

    def dry_run(self, name: str) -> dict[str, Any]:
        return self.call("POST", f"/admin/actions/{name}/dry-run")

    def check(self, name: str) -> str:
        return str(self.call("GET", f"/admin/checks/{name}")["output"])

    def health(self) -> dict[str, Any]:
        return self.call("GET", "/health")

    def call(self, method: str, path: str) -> dict[str, Any]:
        try:
            response = self.client.request(method, path)
        except httpx.HTTPError as error:
            raise SandboxUnavailable(f"sandbox not reachable: {error}") from error
        # /health answers 503 while something is broken; that is a result, not a failure.
        if response.status_code >= 400 and path != "/health":
            raise SandboxUnavailable(f"sandbox answered {response.status_code} for {path}")
        body: dict[str, Any] = response.json()
        return body


@dataclass(frozen=True)
class Executor:
    database_url: str
    sandbox: ShopLite
    actions: frozenset[str]
    checks: frozenset[str]

    def step(self, doc_id: str, version: int, section: Section, ordinal: int) -> SopStep | None:
        with psycopg.connect(self.database_url) as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(STEP, (doc_id, version, section, ordinal)).fetchone()
        return SopStep.model_validate(row) if row else None

    def run(self, doc_id: str, version: int, section: Section, ordinal: int) -> StepResult:
        # The step is read again here, so nothing the chat sends can change what runs. Rootline
        # never changes the system: checks run, and actions are only tried on a copy.
        step = self.step(doc_id, version, section, ordinal)
        if step is None:
            raise LookupError(f"{doc_id} v{version} has no {section} step {ordinal}")
        if step.action:
            return self.dry_run(step.action)
        if step.check_name:
            return self.check(step.check_name)
        raise LookupError(f"{doc_id} v{version} {section} step {ordinal} has nothing to run")

    def dry_run(self, name: str) -> StepResult:
        # Applied to a copy of the sandbox's state, so it shows the effect and changes nothing.
        if name not in self.actions:
            return StepResult(kind="action", name=name, output="", met=False, note="not allowed")
        done = self.sandbox.dry_run(name)
        health = done["health"]
        output = redact(f"{name} on a copy: fixed {', '.join(done['fixed']) or 'nothing'}").text
        if not done["fixed"]:
            return StepResult(
                kind="action", name=name, output=output, met=None, note="it fixes nothing broken"
            )
        met = all(health["components"].get(part) == "ok" for part in done["components"])
        note = "health would be ok after it" if met else "health would still report a failure"
        return StepResult(
            kind="action", name=name, output=output, met=met, note=note, fixed=done["fixed"]
        )

    def check(self, name: str) -> StepResult:
        if name not in self.checks:
            return StepResult(kind="check", name=name, output="", met=False, note="not allowed")
        output = redact(self.sandbox.check(name)).text
        return StepResult(kind="check", name=name, output=output, met=None, note="read-only check")
