import multiprocessing
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import psycopg

from rootline.config import Settings
from rootline.ingest.factory import build_analyzer
from rootline.ingest.store import Outcome, Store

CORPUS = Path(__file__).parents[1] / "corpus"
TABLES = ("documents", "document_versions", "aliases", "chunks", "sop_steps")
PARALLEL_FILES = [
    "greenfield/sop-cache-001.md",
    "brownfield/cache-down-runbook-copy.md",
    "brownfield/orders-worker-oom-runbook.html",
    "brownfield/re-checkout-401s-tonight.eml",
    "reference/ref-pg-001.md",
]
PROCESSES = 8
DUPLICATE_SOURCE_KEYS = (
    "select count(*) from (select source_key from documents group by 1 having count(*) > 1) t"
)
STALE_CHUNKS = (
    "select count(*) from chunks c join documents d using (doc_id)"
    " where c.version <> d.current_version"
)


def ingest(store: Store, relative: str) -> Outcome:
    return store.ingest(f"corpus/{relative}", (CORPUS / relative).read_bytes())


def count(conn: psycopg.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {table: count(conn, f"select count(*) from {table}") for table in TABLES}


def ingest_in_own_process(job: tuple[str, str]) -> list[str]:
    db_url, raw_dir = job
    settings = Settings(database_url=db_url, google_api_key="")
    with psycopg.connect(db_url, autocommit=True) as conn:
        store = Store(conn, build_analyzer(settings), Path(raw_dir), settings.embed_model)
        return [ingest(store, relative).action for relative in PARALLEL_FILES]


def test_ingesting_the_same_file_twice_changes_nothing(store: Store) -> None:
    first = ingest(store, "greenfield/sop-db-001.docx")
    before = table_counts(store.conn)

    second = ingest(store, "greenfield/sop-db-001.docx")

    assert (first.action, second.action) == ("created", "unchanged")
    assert table_counts(store.conn) == before


def test_parallel_processes_ingesting_the_same_files_create_no_duplicates(
    migrated_db: psycopg.Connection, db_url: str, tmp_path: Path
) -> None:
    spawn = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(PROCESSES, mp_context=spawn) as pool:
        results = pool.map(ingest_in_own_process, [(db_url, str(tmp_path))] * PROCESSES)
        actions = Counter(action for result in results for action in result)

    # Five files: four become documents (the mixed page splits into two) and one is an exact
    # copy, so it becomes an alias. Every other attempt finds the work already done.
    assert actions == {"created": 4, "alias": 1, "unchanged": PROCESSES * 5 - 5}
    counts = table_counts(migrated_db)
    assert (counts["documents"], counts["document_versions"], counts["aliases"]) == (5, 5, 1)
    assert count(migrated_db, DUPLICATE_SOURCE_KEYS) == 0
    assert count(migrated_db, STALE_CHUNKS) == 0


def test_edited_file_becomes_version_2_and_only_version_2_is_searchable(store: Store) -> None:
    original = (CORPUS / "greenfield" / "sop-cache-001.md").read_text()
    edited = original.replace("redis-cli -h cache ping", "redis-cli -h cache -p 6380 ping")
    store.ingest("edit/sop-cache-001.md", original.encode())

    outcome = store.ingest("edit/sop-cache-001.md", edited.encode())

    conn = store.conn
    assert outcome.action == "updated"
    assert count(conn, "select current_version from documents") == 2
    assert count(conn, "select count(*) from chunks where version <> 2") == 0
    assert count(conn, "select count(*) from chunks where body like '%-p 6380 ping%'") > 0
    assert count(conn, "select count(*) from chunks where body like '%-h cache ping%'") == 0
    assert (
        count(conn, "select count(*) from sop_steps where command = 'redis-cli -h cache ping'") == 0
    )


def test_deleted_file_disappears_everywhere(store: Store) -> None:
    ingest(store, "greenfield/sop-cache-001.md")
    hits = "select count(*) from chunks where tsv @@ websearch_to_tsquery('simple', 'PONG')"
    assert count(store.conn, hits) > 0

    outcome = store.delete("corpus/greenfield/sop-cache-001.md")

    assert outcome.action == "deleted"
    assert count(store.conn, hits) == 0
    assert count(store.conn, "select count(*) from sop_steps") == 0
    assert count(store.conn, "select count(*) from documents where status <> 'deleted'") == 0
