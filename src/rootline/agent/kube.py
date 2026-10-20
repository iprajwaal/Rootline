import json
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from rootline.agent.executor import SandboxUnavailable, StepResult
from rootline.catalog import Catalog

Run = Callable[[list[str], float], subprocess.CompletedProcess[str]]
# The catalog writes checks as an engineer types them inside the cluster. From outside, the
# same checks run through kubectl exec.
IN_CLUSTER = {
    "db_connections": [
        "exec", "statefulset/orders-db", "--", "psql", "-U", "shop", "-d", "orders",
        "-c", "select count(*) from pg_stat_activity",
    ],
    "cache_ping": ["exec", "statefulset/cache", "--", "redis-cli", "ping"],
    "queue_lag": [
        "exec", "statefulset/order-queue", "--", "rpk", "group", "describe", "orders-worker",
    ],
}  # fmt: skip
WORKLOAD = re.compile(r"^(?:deploy|deployment|statefulset|sts)/[\w-]+$")
# Logs are long; the reply shows the newest lines, where the trouble is.
LOG_LINES = 40


def in_cluster(name: str, line: str, context: str) -> list[str]:
    target = ["--context", context] if context else []
    if name in IN_CLUSTER:
        return ["kubectl", *target, "-n", "shop", *IN_CLUSTER[name]]
    argv = shlex.split(line)
    if argv[0] != "kubectl":
        raise SandboxUnavailable(f"{name} has no counterpart in the cluster")
    return ["kubectl", *target, *argv[1:]]


def run_command(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise SandboxUnavailable("kubectl is not installed here") from error
    except subprocess.TimeoutExpired as error:
        raise SandboxUnavailable(f"{argv[0]} took longer than {timeout:.0f}s") from error


@dataclass(frozen=True)
class KubeShop:
    # The shop on Kubernetes. Only catalog commands run. Checks and logs use a reading
    # account; only a dry run of a fix, and a fix an engineer approved, use the fixing one.
    catalog: Catalog
    health_url: str
    timeout_s: float
    context: str = ""
    fix_context: str = ""
    run: Run = run_command

    def check(self, name: str) -> str:
        if name == "health":
            return json.dumps(self.health())
        done = self.kubectl(in_cluster(name, self.catalog.checks[name], self.context))
        return tail(done.stdout or done.stderr, LOG_LINES)

    def health(self) -> dict[str, Any]:
        try:
            body: dict[str, Any] = httpx.get(self.health_url, timeout=10).json()
            return body
        except (httpx.HTTPError, ValueError):
            # A crashing service answers nothing; that is the finding, not an error here.
            return {"status": "degraded", "components": {"orders-api": "unreachable"}}

    def trial(self, name: str) -> StepResult:
        line = self.catalog.actions[name]
        if "-delete" in line.split():
            # find without -delete lists exactly what the real command would remove, and
            # only needs to read.
            argv = in_cluster(name, line, self.context)
            files = self.kubectl([a for a in argv if a != "-delete"]).stdout.split()
            if not files:
                note = "a preview shows it would delete nothing"
                return result(name, "", None, note, "cluster")
            note = f"a preview shows it would delete {len(files)} files, nothing deleted yet"
            return result(name, "\n".join(files[:20]), True, note, "cluster")
        # The server checks a dry run as if it were real, so it needs the fixing account.
        done = self.kubectl([*in_cluster(name, line, self.fix_context), "--dry-run=server"])
        if done.returncode:
            return result(name, done.stderr, None, "the cluster rejected it", "cluster")
        note = "the cluster accepted it in a dry run, nothing changed yet"
        return result(name, done.stdout, True, note, "cluster")

    def act(self, name: str) -> StepResult:
        argv = in_cluster(name, self.catalog.actions[name], self.fix_context)
        done = self.kubectl(argv)
        lines = [f"$ {shlex.join(argv)}", done.stdout.strip() or done.stderr.strip()]
        if done.returncode:
            return result(name, "\n".join(lines), False, "the command failed", "ran")
        # A changed deployment or statefulset rolls out new pods; health means something only
        # once that has finished.
        workload = next((a for a in argv if WORKLOAD.match(a)), None)
        if workload:
            status = ["kubectl", *self.namespaced(argv), "rollout", "status", workload]
            rolled = self.kubectl([*status, "--timeout=180s"])
            lines.append(rolled.stdout.strip() or rolled.stderr.strip())
        health = self.health()
        failing = sorted(part for part, state in health["components"].items() if state != "ok")
        lines.append(f"health: {health['status']}")
        note = "health is ok again" if not failing else f"still failing: {', '.join(failing)}"
        return result(name, "\n".join(lines), not failing, note, "ran")

    def namespaced(self, argv: list[str]) -> list[str]:
        flags = ["--context", self.context] if self.context else []
        namespace = argv[argv.index("-n") + 1] if "-n" in argv else "shop"
        return [*flags, "-n", namespace]

    def kubectl(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self.run(argv, self.timeout_s)


def result(
    name: str, output: str, met: bool | None, note: str, mode: Literal["cluster", "ran"]
) -> StepResult:
    return StepResult(kind="action", name=name, output=output, met=met, note=note, mode=mode)


def tail(text: str, lines: int) -> str:
    kept = text.strip().splitlines()
    if len(kept) <= lines:
        return "\n".join(kept)
    return "\n".join([f"... {len(kept) - lines} earlier lines", *kept[-lines:]])
