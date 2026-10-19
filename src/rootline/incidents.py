import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from rootline.retrieval.suggest import Suggester, SuggestResponse

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


class AlertmanagerWebhook(BaseModel):
    alerts: list[Alert]


class IncidentResult(BaseModel):
    incident_id: int | None
    fingerprint: str
    alert_name: str
    action: Literal["opened", "repeated", "resolved", "not_open"]
    thread_id: str | None = None
    suggestions: SuggestResponse | None = None


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
