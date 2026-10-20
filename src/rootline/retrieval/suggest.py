import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from rootline.ingest.facts import error_signatures
from rootline.retrieval.issue import Issue, clean, compose_query, error_lines
from rootline.retrieval.search import LANES, SIMILARITY_TIE, Hit, Lane, Searcher

PER_LANE = 3
# Sharing the fired alert or the same error says more than sharing words.
ALERT_WEIGHT = 1.3
ERROR_WEIGHT = 1.15
LEAD = {"rca": "Past incident", "sop": "Procedure"}
NONE_FOUND = {
    "rca": "No past incident matches this issue.",
    "sop": "No procedure covers this issue.",
}
DOCUMENTED_ALERTS = """
    select distinct unnest(alerts) as alert from documents
    where status <> 'deleted' and alerts && %s::text[]
"""
FACTS = """
    select d.doc_id, d.alerts, coalesce(v.facts -> 'error_signatures', '[]') as errors
    from documents d
    left join document_versions v on v.doc_id = d.doc_id and v.version = d.current_version
    where d.doc_id = any(%s)
"""


class Suggestion(Hit):
    why: str


class SuggestionLane(BaseModel):
    suggestions: list[Suggestion]
    ambiguous: bool = False
    nothing_relevant: bool = False


class SuggestResponse(BaseModel):
    issue: Issue
    rca: SuggestionLane
    sop: SuggestionLane
    message: str = ""
    latency_ms: float = 0.0


@dataclass(frozen=True)
class Shared:
    alerts: list[str]
    errors: list[str]
    services: list[str]

    @property
    def weight(self) -> float:
        return (ALERT_WEIGHT if self.alerts else 1.0) * (ERROR_WEIGHT if self.errors else 1.0)


@dataclass(frozen=True)
class Suggester:
    searcher: Searcher
    alerts: dict[str, str]

    def suggest(
        self, text: str, alerts: Sequence[str] = (), services: Sequence[str] = ()
    ) -> SuggestResponse:
        started = time.perf_counter()
        issue = self.read(text, alerts, services)
        found = self.searcher.hybrid(issue.query)
        lanes = {lane: getattr(found, lane).hits for lane in LANES}
        facts = self.facts([hit.doc_id for hits in lanes.values() for hit in hits])
        result = {lane: self.lane(lane, hits, issue, facts) for lane, hits in lanes.items()}
        notes = [NONE_FOUND[lane] for lane in LANES if result[lane].nothing_relevant]
        if notes and issue.undocumented_alerts:
            notes.append(f"No document lists the alert {', '.join(issue.undocumented_alerts)}.")
        message = " ".join(notes)
        return SuggestResponse(
            issue=issue,
            rca=result["rca"],
            sop=result["sop"],
            message=message,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    def read(self, text: str, alerts: Sequence[str], services: Sequence[str]) -> Issue:
        lines = clean(text)
        cleaned = "\n".join(lines)
        fired = list(dict.fromkeys([*alerts, *(a for a in self.alerts if a in cleaned)]))
        # Label values and the alert's own service are mapped to catalog names like the text.
        hints = [*services, *(self.alerts[a] for a in fired if a in self.alerts), cleaned]
        named = sorted({name for hint in hints for name in self.searcher.named_services(hint)})
        errors = error_signatures(cleaned)
        # An alert no document lists cannot match a word, and its name (OrdersApiDiskFull)
        # only pulls in every document about the same service. Service names stay out for
        # the same reason.
        undocumented = sorted(set(fired) - self.documented(fired))
        return Issue(
            alerts=fired,
            undocumented_alerts=undocumented,
            services=named,
            errors=errors,
            text=cleaned,
            query=compose_query([*fired, *error_lines(lines, errors)], drop=undocumented),
        )

    def documented(self, alerts: list[str]) -> set[str]:
        if not alerts:
            return set()
        return {row["alert"] for row in self.searcher.rows(DOCUMENTED_ALERTS, (alerts,))}

    def facts(self, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {row["doc_id"]: row for row in self.searcher.rows(FACTS, (doc_ids,))}

    def lane(
        self, lane: Lane, hits: list[Hit], issue: Issue, facts: dict[str, dict[str, Any]]
    ) -> SuggestionLane:
        if not hits:
            return SuggestionLane(suggestions=[], nothing_relevant=True)
        ranked: list[tuple[Shared, Suggestion]] = []
        for hit in hits:
            doc = facts[hit.doc_id]
            shared = Shared(
                alerts=[a for a in issue.alerts if a in doc["alerts"]],
                errors=shared_errors(issue.errors, doc["errors"]),
                services=[s for s in issue.services if s in hit.services],
            )
            update = {"score": round(hit.score * shared.weight, 5), "why": why(lane, hit, shared)}
            ranked.append((shared, Suggestion.model_validate(hit.model_dump() | update)))
        # When some documents share the alert or the error, the ones sharing only words are noise.
        if any(shared.alerts or shared.errors for shared, _ in ranked):
            ranked = [(shared, s) for shared, s in ranked if shared.alerts or shared.errors]
        elif issue.errors:
            # A named error no document has seen is a new problem, however close the wording.
            return SuggestionLane(suggestions=[], nothing_relevant=True)
        ranked.sort(key=lambda pair: (pair[1].status == "superseded", -pair[1].score))
        ranked = ranked[:PER_LANE]
        # A superseded document next to its replacement is not a second answer.
        current = [pair for pair in ranked if pair[1].status != "superseded"]
        tied = (
            len(current) > 1
            and current[0][0].weight == current[1][0].weight
            and abs(current[0][1].similarity - current[1][1].similarity) < SIMILARITY_TIE
        )
        return SuggestionLane(suggestions=[s for _, s in ranked], ambiguous=tied)


def error_name(signature: str) -> str:
    # "psycopg_pool.PoolTimeout: couldn't get..." and "PoolTimeout" are the same error.
    head = re.split(r"[:(]", signature, maxsplit=1)[0].strip()
    return head.rsplit(".", 1)[-1]


def shared_errors(issue_errors: list[str], doc_errors: list[str]) -> list[str]:
    known = {error_name(error).lower() for error in doc_errors}
    names = [error_name(error) for error in issue_errors]
    return list(dict.fromkeys(name for name in names if name.lower() in known))


def why(lane: Lane, hit: Hit, shared: Shared) -> str:
    parts = [
        f"{kind} {', '.join(values)}"
        for kind, values in (
            ("alert", shared.alerts),
            ("error", shared.errors),
            ("service", shared.services),
        )
        if values
    ]
    if parts:
        reason = f"{LEAD[lane]} with the same {join_words(parts)}."
    else:
        reason = f"{LEAD[lane]} with similar wording, similarity {hit.similarity:.2f}."
    newer = [link.doc_id for link in hit.linked if link.relation == "superseded_by"]
    if hit.status == "superseded":
        reason += f" Superseded by {', '.join(newer)}." if newer else " Superseded."
    if hit.needs_review:
        reason += " Not reviewed yet."
    return reason


def join_words(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
