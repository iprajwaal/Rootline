from dataclasses import dataclass
from typing import Any, Literal, Protocol

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
    # For a fix tried first: True when it would help, False when it would help only in part,
    # None when it would change nothing. After a real run: whether health is ok again. Always
    # None for a read-only check.
    met: bool | None
    note: str
    fixed: list[str] = []
    # copy: tried on a copy of the sandbox · cluster: checked by the cluster without
    # changing it · ran: really run after an engineer approved it.
    mode: Literal["copy", "cluster", "ran"] = "copy"


class Sandbox(Protocol):
    # What the steps act on: the ShopLite sandbox or the shop on Kubernetes.
    def check(self, name: str) -> str: ...

    def health(self) -> dict[str, Any]: ...

    def trial(self, name: str) -> StepResult: ...

    def act(self, name: str) -> StepResult: ...


@dataclass(frozen=True)
class ShopLite:
    client: httpx.Client

    def check(self, name: str) -> str:
        return str(self.call("GET", f"/admin/checks/{name}")["output"])

    def health(self) -> dict[str, Any]:
        return self.call("GET", "/health")

    def trial(self, name: str) -> StepResult:
        # Applied to a copy of the sandbox's state, so it shows the effect and changes nothing.
        done = self.call("POST", f"/admin/actions/{name}/dry-run")
        return verdict(name, done, done["health"], "copy")

    def act(self, name: str) -> StepResult:
        done = self.call("POST", f"/admin/actions/{name}")
        return verdict(name, done, self.health(), "ran")

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


def verdict(
    name: str, done: dict[str, Any], health: dict[str, Any], mode: Literal["copy", "ran"]
) -> StepResult:
    copy = mode == "copy"
    output = f"{name}{' on a copy' if copy else ''}: fixed {', '.join(done['fixed']) or 'nothing'}"
    if not done["fixed"]:
        return StepResult(
            kind="action", name=name, output=output, met=None, note="it fixes nothing broken",
            mode=mode,
        )  # fmt: skip
    met = all(health["components"].get(part) == "ok" for part in done["components"])
    if copy:
        note = "health would be ok after it" if met else "health would still report a failure"
    else:
        note = "health is ok again" if met else "health still reports a failure"
    return StepResult(
        kind="action", name=name, output=output, met=met, note=note, fixed=done["fixed"],
        mode=mode,
    )  # fmt: skip


@dataclass(frozen=True)
class Executor:
    database_url: str
    sandbox: Sandbox
    actions: frozenset[str]
    checks: frozenset[str]

    def step(self, doc_id: str, version: int, section: Section, ordinal: int) -> SopStep | None:
        with psycopg.connect(self.database_url) as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(STEP, (doc_id, version, section, ordinal)).fetchone()
        return SopStep.model_validate(row) if row else None

    def run(self, doc_id: str, version: int, section: Section, ordinal: int) -> StepResult:
        # The step is read again here, so nothing the chat sends can change what runs. A check
        # runs; a fix is only tried, and runs for real through act() after an approval.
        step = self.step(doc_id, version, section, ordinal)
        if step is None:
            raise LookupError(f"{doc_id} v{version} has no {section} step {ordinal}")
        if step.action:
            return self.dry_run(step.action)
        if step.check_name:
            return self.check(step.check_name)
        raise LookupError(f"{doc_id} v{version} {section} step {ordinal} has nothing to run")

    def dry_run(self, name: str) -> StepResult:
        if name not in self.actions:
            return refused("action", name)
        return redacted(self.sandbox.trial(name))

    def act(self, name: str) -> StepResult:
        # Only catalog actions, and only after an engineer approved this one in the chat.
        if name not in self.actions:
            return refused("action", name)
        return redacted(self.sandbox.act(name))

    def check(self, name: str) -> StepResult:
        if name not in self.checks:
            return refused("check", name)
        output = redact(self.sandbox.check(name)).text
        return StepResult(kind="check", name=name, output=output, met=None, note="read-only check")


def refused(kind: Literal["action", "check"], name: str) -> StepResult:
    return StepResult(kind=kind, name=name, output="", met=False, note="not allowed")


def redacted(result: StepResult) -> StepResult:
    return result.model_copy(update={"output": redact(result.output).text})
