import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from confluent_kafka import Consumer, TopicPartition

logging.basicConfig(format="%(asctime)s %(levelname)s [alerter] %(message)s", level="INFO")
# Every request would otherwise be logged; only the problems matter here.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("alerter")

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
NAMESPACE = "shop"


@dataclass(frozen=True)
class Rule:
    alert: str
    service: str
    severity: str
    # Words that pick this fault's line out of the orders-api logs.
    marker: str


# Names match corpus/catalog.yaml, so Rootline links each alert to its documents.
RULES = {
    "db": Rule("OrdersDbPoolExhausted", "orders-api", "critical", "PoolTimeout"),
    "disk": Rule("OrdersApiDiskFull", "orders-api", "critical", "No space left on device"),
    "cache": Rule("OrdersApiHighLatency", "cache", "critical", "redis.exceptions"),
    "crashloop": Rule("OrdersApiCrashLooping", "orders-api", "critical", "config error"),
    "lag": Rule("OrderQueueConsumerLagHigh", "orders-worker", "warning", "lag="),
}


@dataclass
class Alerter:
    # Probes the real shop and posts Alertmanager webhooks to Rootline. A problem must hold
    # for several probes before it fires, so a normal rollout never pages anyone.
    kube: httpx.Client
    shop: httpx.Client
    webhook: str
    hold: int
    queue: Consumer
    lag_threshold: int
    lag: int = 0
    seen: dict[str, int] = field(default_factory=dict)
    sent: dict[str, str] = field(default_factory=dict)

    def tick(self) -> None:
        failing = self.failing()
        for part, rule in RULES.items():
            self.seen[part] = self.seen.get(part, 0) + 1 if part in failing else 0
            status = "firing" if self.seen[part] >= self.hold else "resolved"
            if status == "resolved" and part in failing:
                continue
            # The first tick after a start announces every state, so an alert left open by a
            # restart is resolved instead of staying open forever.
            if self.sent.get(part) == status:
                continue
            line = self.error_line(rule) if status == "firing" else ""
            try:
                response = self.post(alert(rule, status, line))
                response.raise_for_status()
                self.sent[part] = status
                log.info("%s %s: %s", rule.alert, status, line or "-")
            except httpx.HTTPError as error:
                # The receiver may have stored it anyway, so the next tick sends it again.
                self.sent.pop(part, None)
                log.warning("alert %s not delivered: %s", rule.alert, error)

    def failing(self) -> set[str]:
        found = set()
        try:
            health = self.shop.get("/health", timeout=10).json()
            found |= {part for part, state in health["components"].items() if state != "ok"}
        except (httpx.HTTPError, ValueError):
            # Unreachable alone is often a rollout; the pod status says if it is a crash loop.
            pass
        if self.crash_looping():
            found.add("crashloop")
        self.lag = self.consumer_lag()
        if self.lag > self.lag_threshold:
            found.add("lag")
        return found & set(RULES)

    def consumer_lag(self) -> int:
        # What the orders-worker group still has to read, summed over the topic's partitions.
        try:
            topic = self.queue.list_topics("orders", timeout=5).topics["orders"]
            parts = [TopicPartition("orders", p) for p in topic.partitions]
            committed = self.queue.committed(parts, timeout=5)
        except Exception as error:
            log.warning("lag not measured: %s", error)
            return 0
        lag = 0
        for part in committed:
            low, high = self.queue.get_watermark_offsets(part, timeout=5)
            lag += high - (part.offset if part.offset >= 0 else low)
        return lag

    def crash_looping(self) -> bool:
        # A crash-looping container swings between back-off and a brief exit, so its state
        # alone flaps. Restarting again and again without ever becoming ready does not.
        for pod in self.pods():
            for container in pod["status"].get("containerStatuses", []):
                if container.get("restartCount", 0) >= 2 and not container.get("ready"):
                    return True
        return False

    def error_line(self, rule: Rule) -> str:
        # The newest error the service actually logged for this fault, from the running
        # container or, after a crash, the one before it. Lag is measured, not logged.
        if rule.alert == RULES["lag"].alert:
            return (
                f"consumer group orders-worker lag={self.lag} on topic orders"
                f" exceeds threshold={self.lag_threshold}"
            )
        for pod in self.pods():
            for previous in ("false", "true"):
                try:
                    text = self.kube.get(
                        f"/api/v1/namespaces/{NAMESPACE}/pods/{pod['metadata']['name']}/log",
                        params={"previous": previous, "sinceSeconds": 300, "tailLines": 500},
                    ).text
                except httpx.HTTPError:
                    continue
                lines = [line for line in text.splitlines() if rule.marker in line]
                if lines:
                    return lines[-1].split("] ", 1)[-1].strip()
        return f"{rule.alert} on {rule.service}"

    def pods(self) -> list[dict[str, Any]]:
        try:
            listed = self.kube.get(
                f"/api/v1/namespaces/{NAMESPACE}/pods", params={"labelSelector": "app=orders-api"}
            )
            items: list[dict[str, Any]] = listed.json()["items"]
            return items
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    def post(self, payload: dict[str, Any]) -> httpx.Response:
        return httpx.post(self.webhook, json=payload, timeout=30)


def alert(rule: Rule, status: str, line: str) -> dict[str, Any]:
    fingerprint = hashlib.sha256(f"{rule.alert}:{rule.service}".encode()).hexdigest()[:16]
    return {
        "version": "4",
        "status": status,
        "receiver": "rootline",
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": rule.alert,
                    "service": rule.service,
                    "severity": rule.severity,
                },
                "annotations": {"summary": f"{rule.alert} on {rule.service}", "description": line},
                "startsAt": datetime.now(UTC).isoformat(),
                "fingerprint": fingerprint,
            }
        ],
    }


def main() -> None:
    kube = httpx.Client(
        base_url="https://kubernetes.default.svc",
        headers={"Authorization": f"Bearer {(ACCOUNT / 'token').read_text()}"},
        verify=str(ACCOUNT / "ca.crt"),
        timeout=10,
    )
    alerter = Alerter(
        kube=kube,
        shop=httpx.Client(base_url=os.environ.get("TARGET", "http://orders-api")),
        webhook=os.environ.get("WEBHOOK_URL", "http://host.docker.internal:8000/incidents"),
        hold=int(os.environ.get("HOLD_PROBES", "2")),
        # Joins no group: it only reads the worker group's committed offsets.
        queue=Consumer(
            {"bootstrap.servers": os.environ["KAFKA_BROKERS"], "group.id": "orders-worker"}
        ),
        lag_threshold=int(os.environ.get("LAG_THRESHOLD", "300")),
    )
    every = float(os.environ.get("PROBE_SECONDS", "5"))
    log.info("probing orders-api every %ss, alerts to %s", every, alerter.webhook)
    while True:
        alerter.tick()
        time.sleep(every)


if __name__ == "__main__":
    main()
