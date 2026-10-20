from pydantic import BaseModel

from rootline.agent.executor import Executor, SandboxUnavailable

LOG_LINES = 8


class Finding(BaseModel):
    check: str
    output: str


def triage(executor: Executor) -> list[Finding]:
    # Read-only only: overall health, the check for each failing part, then recent logs.
    try:
        health = executor.sandbox.health()
        failing = sorted(part for part, state in health["components"].items() if state != "ok")
        findings = [
            Finding(
                check="health",
                output=f"{health['status']}, failing: {', '.join(failing) or 'nothing'}",
            )
        ]
        for name in sorted(executor.checks):
            if any(name.startswith(part) for part in failing):
                findings.append(Finding(check=name, output=executor.check(name).output))
        logs = executor.check("logs").output.splitlines()[-LOG_LINES:]
    except SandboxUnavailable:
        return []
    return [*findings, Finding(check="logs", output="\n".join(logs))] if logs else findings
