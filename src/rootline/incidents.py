import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
import psycopg
from langgraph_sdk import get_sync_client
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from rootline.retrieval.suggest import Suggester, SuggestResponse

log = logging.getLogger(__name__)

TEXT_ANNOTATIONS = ("summary", "description", "message")
SERVICE_LABELS = ("service", "app", "job", "deployment", "container")

# xmax is 0 only on a freshly inserted row, so it tells a new incident from a repeat.
OPEN = """
    insert into incidents (fingerprint, service, alert_name, payload)
    values (%(fingerprint)s, %(service)s, %(alert)s, %(payload)s)
    on conflict (fingerprint) where status = 'open'
    do update set fire_count = incidents.fire_count + 1
    returning id, thread_id, suggestions, (xmax = 0) as opened
"""
RESOLVE = """
    update incidents set status = 'resolved', resolved_at = now()
    where fingerprint = %s and status = 'open'
    returning id, thread_id
"""
RECENT = """
    select id, alert_name, service, status, fire_count, created_at, resolved_at, thread_id,
        chat_opened_at is not null as chat_opened, suggestions
    from incidents where %(all)s or status = 'open'
    order by created_at desc limit %(n)s
"""
# Only the first click claims the incident, so two clicks never start two chat runs.
CLAIM = """
    update incidents set chat_opened_at = now()
    where id = %s and chat_opened_at is null
    returning thread_id, payload
"""


class Alert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    fingerprint: str = ""

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "")

    def key(self) -> str:
        # Alertmanager sends a fingerprint; other senders get one from the labels.
        labels = json.dumps(self.labels, sort_keys=True).encode()
        return self.fingerprint or hashlib.sha256(labels).hexdigest()[:16]

    def text(self) -> str:
        notes = [self.annotations[key] for key in TEXT_ANNOTATIONS if key in self.annotations]
        return "\n".join([self.name, *notes])

    def services(self) -> list[str]:
        return [self.labels[key] for key in SERVICE_LABELS if key in self.labels]

    def chat_text(self) -> str:
        notes = [self.annotations[key] for key in TEXT_ANNOTATIONS if key in self.annotations]
        return "\n".join([f"[FIRING:1] {self.name} {' '.join(self.services())}".strip(), *notes])


class AlertmanagerWebhook(BaseModel):
    alerts: list[Alert]


class IncidentResult(BaseModel):
    incident_id: int | None
    fingerprint: str
    alert_name: str
    action: Literal["opened", "repeated", "resolved", "not_open"]
    thread_id: str | None = None
    suggestions: SuggestResponse | None = None


class IncidentView(BaseModel):
    id: int
    alert_name: str | None
    service: str | None
    status: str
    fire_count: int
    created_at: datetime
    resolved_at: datetime | None
    thread_id: str | None
    chat_opened: bool
    top_rca: str | None
    top_sop: str | None


class AgentUnavailable(RuntimeError):
    pass


# Opens the incident's chat thread on the agent server with the alert as its first message.
OpenThread = Callable[[str, str], None]
# The graph name in langgraph.json. The chat UI lists threads by it, so alert threads carry it.
AGENT_GRAPH = "agent"


@dataclass(frozen=True)
class Incidents:
    conn: psycopg.Connection[Any]
    suggester: Suggester

    def receive(self, webhook: AlertmanagerWebhook) -> list[IncidentResult]:
        return [
            self.resolve(alert) if alert.status == "resolved" else self.open(alert)
            for alert in webhook.alerts
        ]

    def open(self, alert: Alert) -> IncidentResult:
        service = next(iter(alert.services()), None) or self.suggester.alerts.get(alert.name)
        params = {
            "fingerprint": alert.key(),
            "service": service,
            "alert": alert.name,
            "payload": Jsonb(alert.model_dump()),
        }
        row = self.row(OPEN, params)
        assert row, "an upsert always returns its row"
        if row["suggestions"]:
            found = SuggestResponse.model_validate(row["suggestions"])
        else:
            # Also retried on a repeat, in case the first try failed after the insert.
            alerts = [alert.name] if alert.name else []
            found = self.suggester.suggest(alert.text(), alerts, alert.services())
            self.conn.execute(
                "update incidents set suggestions = %s where id = %s",
                (Jsonb(found.model_dump(mode="json")), row["id"]),
            )
        return IncidentResult(
            incident_id=row["id"],
            fingerprint=alert.key(),
            alert_name=alert.name,
            action="opened" if row["opened"] else "repeated",
            thread_id=row["thread_id"],
            suggestions=found,
        )

    def resolve(self, alert: Alert) -> IncidentResult:
        row = self.row(RESOLVE, (alert.key(),))
        return IncidentResult(
            incident_id=row["id"] if row else None,
            fingerprint=alert.key(),
            alert_name=alert.name,
            action="resolved" if row else "not_open",
            thread_id=row["thread_id"] if row else None,
        )

    def row(self, sql: str, params: Any) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(sql, params).fetchone()


def recent_incidents(
    conn: psycopg.Connection[Any], everything: bool, limit: int
) -> list[IncidentView]:
    with conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(RECENT, {"all": everything, "n": limit}).fetchall()
    return [
        IncidentView.model_validate(
            row
            | {"top_rca": top(row["suggestions"], "rca"), "top_sop": top(row["suggestions"], "sop")}
        )
        for row in rows
    ]


def top(suggestions: dict[str, Any] | None, lane: str) -> str | None:
    found = (suggestions or {}).get(lane, {}).get("suggestions", [])
    return f"{found[0]['doc_id']} · {found[0]['title']}" if found else None


def open_chat(
    conn: psycopg.Connection[Any], incident_id: int, open_thread: OpenThread
) -> str | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        claimed = cursor.execute(CLAIM, (incident_id,)).fetchone()
        if claimed is None:
            row = cursor.execute(
                "select thread_id from incidents where id = %s", (incident_id,)
            ).fetchone()
            return row["thread_id"] if row else None
    try:
        open_thread(claimed["thread_id"], Alert.model_validate(claimed["payload"]).chat_text())
    except AgentUnavailable:
        # Released again, so the next click can retry.
        conn.execute("update incidents set chat_opened_at = null where id = %s", (incident_id,))
        raise
    thread_id: str = claimed["thread_id"]
    return thread_id


def thread_opener(agent_url: str) -> OpenThread:
    def open_thread(thread_id: str, text: str) -> None:
        client = get_sync_client(url=agent_url)
        first: dict[str, Any] = {"messages": [{"type": "human", "content": text}]}
        try:
            client.threads.create(
                thread_id=thread_id,
                if_exists="do_nothing",
                metadata={"graph_id": AGENT_GRAPH, "source": "alert"},
            )
            # Waits for the first answer: the chat UI shows a thread's saved state and does not
            # join a run that is still going.
            run = client.runs.create(thread_id, AGENT_GRAPH, input=first)
            client.runs.join(thread_id, run["run_id"])
        except httpx.HTTPError as error:
            log.warning("incident chat not opened thread_id=%s error=%s", thread_id, error)
            raise AgentUnavailable(f"agent server not reachable: {error}") from error

    return open_thread
