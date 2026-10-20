import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

LOG_LINES = 1000
# The health component each fault breaks.
COMPONENTS = {
    "db_pool_exhausted": "db",
    "queue_consumer_lag": "queue",
    "cache_down": "cache",
    "payment_key_expired": "key",
    "disk_full": "disk",
    "bad_config_deploy": "config",
    "worker_oom": "memory",
}
# Read-only check output: (healthy, while its fault is active). The key check reports
# metadata only, never the key itself.
CHECK_OUTPUT = {
    "db_connections": (
        "count 34 of 100 (pool max 20 per pod, 5 pods)",
        "count 100 of 100 (pool max 20 per pod, 5 pods, every pool is full)",
    ),
    "queue_lag": (
        "group orders-worker lag 120 on 6 partitions",
        "group orders-worker lag 48211 on topic orders, threshold 10000",
    ),
    "cache_ping": ("PONG", "Could not connect to cache:6379: Connection refused"),
    "key_status": (
        "payment-gateway-key valid, expires in 87 days, scopes charge refund",
        "payment-gateway-key expired 2 hours ago, scopes charge refund",
    ),
    "disk_usage": ("/var/lib/shoplite 41% used", "/var/lib/shoplite 100% used"),
    "config_diff": (
        "revision 3 matches revision 2",
        "revision 3 removed PAYMENT_GATEWAY_URL",
    ),
    "memory_usage": (
        "orders-worker 310Mi of 512Mi, 0 restarts",
        "orders-worker 509Mi of 512Mi, 4 restarts (OOMKilled)",
    ),
}


@dataclass(frozen=True)
class Fault:
    name: str
    service: str
    alert: str
    severity: str
    error: str
    http_status: int
    reveal_check: str
    fix_action: str

    @property
    def component(self) -> str:
        return COMPONENTS[self.name]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.alert}:{self.service}".encode()).hexdigest()[:16]


@dataclass
class Shop:
    faults: dict[str, Fault]
    actions: set[str]
    checks: set[str]
    active: dict[str, datetime] = field(default_factory=dict)
    logs: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=LOG_LINES))

    def break_with(self, name: str) -> Fault:
        fault = self.faults[name]
        self.active.setdefault(name, datetime.now(UTC))
        self.log("error", fault.service, fault.error)
        return fault

    def reset(self) -> None:
        self.active.clear()
        self.log("info", "orders-api", "all faults cleared")

    def act(self, action: str) -> dict[str, Any]:
        fixed = self.fixes(action)
        for name in fixed:
            del self.active[name]
        self.log(
            "info",
            "orders-api",
            f"admin action {action} done, fixed {', '.join(fixed) or 'nothing'}",
        )
        return {"action": action, "fixed": fixed, "components": [COMPONENTS[n] for n in fixed]}

    def dry_run(self, action: str) -> dict[str, Any]:
        # The action is applied to a copy of the active faults; the live shop is unchanged.
        fixed = self.fixes(action)
        left = [name for name in self.active if name not in fixed]
        return {
            "action": action,
            "fixed": fixed,
            "components": [COMPONENTS[n] for n in fixed],
            "health": health_of(left),
        }

    def fixes(self, action: str) -> list[str]:
        return [
            f.name for f in self.faults.values() if f.fix_action == action and f.name in self.active
        ]

    def check(self, name: str) -> str:
        if name == "health":
            return json.dumps(self.health())
        if name == "logs":
            return "\n".join(
                f"{line['ts']} {line['level'].upper()} [{line['service']}] {line['message']}"
                for line in list(self.logs)[-20:]
            )
        healthy, failing = CHECK_OUTPUT[name]
        broken = any(self.faults[f].reveal_check == name for f in self.active)
        return failing if broken else healthy

    def health(self) -> dict[str, Any]:
        return health_of(list(self.active))

    def log(self, level: str, service: str, message: str) -> None:
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": level,
            "service": service,
            "message": message,
        }
        self.logs.append(line)
        print(json.dumps(line), flush=True)

    def alert(self, fault: Fault, status: str) -> dict[str, Any]:
        started = self.active.get(fault.name, datetime.now(UTC)).isoformat()
        return {
            "version": "4",
            "status": status,
            "receiver": "rootline",
            "alerts": [
                {
                    "status": status,
                    "labels": {
                        "alertname": fault.alert,
                        "service": fault.service,
                        "severity": fault.severity,
                    },
                    "annotations": {
                        "summary": f"{fault.alert} on {fault.service}",
                        "description": fault.error,
                    },
                    "startsAt": started,
                    "fingerprint": fault.fingerprint,
                }
            ],
        }


def health_of(active: list[str]) -> dict[str, Any]:
    broken = {COMPONENTS[name] for name in active}
    components = {part: "failing" if part in broken else "ok" for part in COMPONENTS.values()}
    return {"status": "degraded" if broken else "ok", "components": components}


def load_shop(catalog: Path) -> Shop:
    # Fault, action and check names come from the same catalog Rootline reads, so the two
    # can never drift apart.
    raw: dict[str, Any] = yaml.safe_load(catalog.read_text())
    faults = {
        name: Fault(
            name=name,
            service=fault["service"],
            alert=fault["alert"],
            severity=fault["severity"],
            error=fault["error"],
            http_status=fault["http_status"],
            reveal_check=fault["reveal_check"],
            fix_action=fault["fix_action"],
        )
        for name, fault in raw["faults"].items()
    }
    return Shop(faults=faults, actions=set(raw["actions"]), checks=set(raw["checks"]))
