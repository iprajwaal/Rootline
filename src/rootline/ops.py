import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

log = logging.getLogger(__name__)

INSERT = """
    insert into requests (request_id, kind, path, ms, llm_calls)
    values (%(request_id)s, %(kind)s, %(path)s, %(ms)s, %(llm_calls)s)
    on conflict (request_id) do nothing
"""
# Spend is summed per request first, so a turn with many calls still counts once.
BY_KIND = """
    with spend as (
        select request_id, sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens,
            sum(cost_usd) as cost, count(*) filter (where cardinality(degraded) > 0) as degraded
        from ledger group by request_id
    )
    select r.kind, count(*) as requests,
        round(percentile_cont(0.5) within group (order by r.ms))::int as p50_ms,
        round(percentile_cont(0.95) within group (order by r.ms))::int as p95_ms,
        round(avg(r.llm_calls), 2) as llm_calls,
        round(avg(coalesce(s.input_tokens, 0))) as input_tokens,
        round(avg(coalesce(s.output_tokens, 0))) as output_tokens,
        round(avg(coalesce(s.cost, 0)), 6) as cost_per_request,
        round(sum(coalesce(s.cost, 0)), 4) as cost_total,
        count(*) filter (where coalesce(s.degraded, 0) > 0) as degraded
    from requests r left join spend s using (request_id)
    where r.at > now() - make_interval(days => %(days)s)
    group by r.kind order by requests desc
"""
BY_TASK = """
    select task, count(*) as calls,
        count(*) filter (where kind = 'llm_cache') as cached,
        count(*) filter (where kind in ('llm_unavailable', 'llm_error')) as failed,
        sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens,
        round(sum(cost_usd), 4) as cost,
        round(avg(latency_ms) filter (where kind = 'llm_call')) as avg_ms
    from ledger where created_at > now() - make_interval(days => %(days)s)
    group by task order by calls desc
"""
DEGRADED = """
    select task, reason, count(*) as calls, max(created_at) as last_seen
    from ledger, unnest(degraded) as reason
    where created_at > now() - make_interval(days => %(days)s)
    group by task, reason order by last_seen desc
"""
BACKLOG = """
    select reason, count(*) as open_items, min(created_at) as oldest
    from review_items where status = 'open' group by reason order by open_items desc
"""


class Turn(BaseModel):
    request_id: str
    kind: str
    path: str
    ms: int
    llm_calls: int


class OpsReport(BaseModel):
    days: int
    by_kind: list[dict[str, Any]]
    by_task: list[dict[str, Any]]
    degraded: list[dict[str, Any]]
    backlog: list[dict[str, Any]]


@dataclass(frozen=True)
class RequestLog:
    database_url: str

    def record(self, turn: Turn) -> None:
        try:
            with psycopg.connect(self.database_url, autocommit=True) as conn:
                conn.execute(INSERT, turn.model_dump())
        except psycopg.Error as error:
            # A lost row must not fail the answer it describes.
            log.warning("request log write failed error=%s", error)


def ops_report(conn: psycopg.Connection[Any], days: int) -> OpsReport:
    with conn.cursor(row_factory=dict_row) as cursor:
        found = {
            name: cursor.execute(sql, {"days": days}).fetchall()
            for name, sql in (("by_kind", BY_KIND), ("by_task", BY_TASK), ("degraded", DEGRADED))
        }
        backlog = cursor.execute(BACKLOG).fetchall()
    return OpsReport(days=days, backlog=backlog, **found)
