import httpx
import psycopg
import pytest
from fake_shop import FakeShop

from rootline.agent.executor import Executor, SandboxUnavailable, ShopLite
from rootline.ingest.analysis import Analyzer


def executor(
    url: str, analyzer: Analyzer, shop: ShopLite, actions: set[str] | None = None
) -> Executor:
    catalog = analyzer.catalog
    allowed = frozenset(catalog.actions if actions is None else actions)
    return Executor(url, shop, allowed, frozenset(catalog.checks))


def test_approved_action_runs_and_health_confirms_the_fix(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    shop = FakeShop()

    result = executor(search_db_url, analyzer, shop.client()).run("SOP-DB-001", 1, "steps", 4)

    assert (result.kind, result.name, result.met) == ("action", "raise_pool_limit", True)
    assert shop.actions() == ["POST /admin/actions/raise_pool_limit"]


def test_action_that_leaves_health_failing_is_not_met(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    result = executor(search_db_url, analyzer, FakeShop(fixes=False).client()).run(
        "SOP-DB-001", 1, "steps", 4
    )

    assert (result.met, result.note) == (False, "health still reports a failure")


def test_action_with_nothing_to_fix_has_nothing_to_verify(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    result = executor(search_db_url, analyzer, FakeShop(broken=False).client()).run(
        "SOP-DB-001", 1, "steps", 4
    )

    assert (result.met, result.note) == (None, "nothing it fixes was broken")


def test_check_step_reads_without_changing_anything(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    shop = FakeShop()

    result = executor(search_db_url, analyzer, shop.client()).run("SOP-DB-001", 1, "steps", 2)

    assert (result.kind, result.name, result.output) == (
        "check",
        "db_connections",
        "count 100 of 100",
    )
    assert shop.actions() == []


def test_action_missing_from_the_allowlist_is_never_sent(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    shop = FakeShop()

    result = executor(search_db_url, analyzer, shop.client(), actions=set()).run(
        "SOP-DB-001", 1, "steps", 4
    )

    assert (result.met, result.note) == (False, "not allowed")
    assert shop.calls == []


def test_sandbox_output_is_redacted(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    shop = FakeShop(detail="key AKIAABCDEFGHIJKLMNOP")

    result = executor(search_db_url, analyzer, shop.client()).run("SOP-DB-001", 1, "steps", 4)

    assert "AKIAABCDEFGHIJKLMNOP" not in result.output


def test_unreachable_sandbox_raises_a_clear_error(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    shop = ShopLite(httpx.Client(transport=httpx.MockTransport(down), base_url="http://shop"))

    with pytest.raises(SandboxUnavailable):
        executor(search_db_url, analyzer, shop).run("SOP-DB-001", 1, "steps", 4)


def test_step_that_does_not_exist_is_reported(
    search_db: psycopg.Connection, search_db_url: str, analyzer: Analyzer
) -> None:
    with pytest.raises(LookupError):
        executor(search_db_url, analyzer, FakeShop().client()).run("SOP-DB-001", 1, "steps", 9)
