import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from shoplite.app import Watchdog, create_app
from shoplite.shop import Shop, load_shop

CATALOG = Path(__file__).parents[3] / "corpus" / "catalog.yaml"


@pytest.fixture
def shop() -> Shop:
    return load_shop(CATALOG)


@pytest.fixture
def client(shop: Shop) -> TestClient:
    return TestClient(create_app(shop, None, 5))


def test_healthy_shop_serves_orders(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/orders").status_code == 200


def test_fault_breaks_its_component_and_the_orders_page(client: TestClient) -> None:
    client.post("/chaos/db_pool_exhausted")

    health = client.get("/health")
    orders = client.get("/orders")

    assert health.status_code == 503 and health.json()["components"]["db"] == "failing"
    assert orders.status_code == 503 and "PoolTimeout" in orders.json()["error"]


def test_check_shows_the_fault_and_the_fix_clears_it(client: TestClient) -> None:
    client.post("/chaos/db_pool_exhausted")
    before = client.get("/admin/checks/db_connections").json()["output"]

    fixed = client.post("/admin/actions/raise_pool_limit").json()

    assert "every pool is full" in before
    assert fixed == {
        "action": "raise_pool_limit",
        "fixed": ["db_pool_exhausted"],
        "components": ["db"],
    }
    assert client.get("/health").json()["status"] == "ok"


def test_dry_run_reports_the_fix_without_changing_the_shop(client: TestClient) -> None:
    client.post("/chaos/disk_full")

    right = client.post("/admin/actions/clear_tmp/dry-run").json()
    wrong = client.post("/admin/actions/restart_api/dry-run").json()

    assert right["fixed"] == ["disk_full"] and right["health"]["status"] == "ok"
    assert wrong["fixed"] == [] and wrong["health"]["components"]["disk"] == "failing"
    assert client.get("/health").json()["components"]["disk"] == "failing"
    assert client.post("/admin/actions/rm_rf/dry-run").status_code == 404


def test_action_for_another_fault_fixes_nothing(client: TestClient) -> None:
    client.post("/chaos/db_pool_exhausted")

    assert client.post("/admin/actions/restart_cache").json()["fixed"] == []
    assert client.get("/health").json()["status"] == "degraded"


def test_unknown_fault_action_and_check_are_404(client: TestClient) -> None:
    assert client.post("/chaos/meteor").status_code == 404
    assert client.post("/admin/actions/rm_rf").status_code == 404
    assert client.get("/admin/checks/everything").status_code == 404


def test_key_check_reports_metadata_never_the_key(client: TestClient) -> None:
    client.post("/chaos/payment_key_expired")

    output = client.get("/admin/checks/key_status").json()["output"]

    assert output == "payment-gateway-key expired 2 hours ago, scopes charge refund"


def test_logs_keep_the_fault_error(client: TestClient) -> None:
    client.post("/chaos/cache_down")

    messages = [line["message"] for line in client.get("/logs").json()]

    assert any("Connection refused" in message for message in messages)


def test_watchdog_posts_firing_once_then_resolved(shop: Shop) -> None:
    sent: list[dict[str, object]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as http:
            watchdog = Watchdog(shop, "http://rootline/incidents", http)
            shop.break_with("db_pool_exhausted")
            await watchdog.tick()
            await watchdog.tick()
            shop.act("raise_pool_limit")
            await watchdog.tick()

    asyncio.run(run())

    pool = [p for p in sent if p["alerts"][0]["labels"]["alertname"] == "OrdersDbPoolExhausted"]  # type: ignore[index]
    assert [payload["status"] for payload in pool] == ["firing", "resolved"]
    assert pool[0]["alerts"][0]["fingerprint"] == pool[1]["alerts"][0]["fingerprint"]  # type: ignore[index]
    # The other faults are announced once as resolved on the first tick, and never again.
    others = [p for p in sent if p not in pool]
    assert len(others) == len(shop.faults) - 1 and {p["status"] for p in others} == {"resolved"}


def test_a_restarted_watchdog_resolves_alerts_it_never_saw_firing(shop: Shop) -> None:
    sent: list[dict[str, object]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as http:
            await Watchdog(shop, "http://rootline/incidents", http).tick()

    asyncio.run(run())

    names = {p["alerts"][0]["labels"]["alertname"] for p in sent}  # type: ignore[index]
    assert {p["status"] for p in sent} == {"resolved"} and "OrdersApiDiskFull" in names


def test_undelivered_alert_is_sent_again_on_the_next_tick(shop: Shop) -> None:
    disk: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["alerts"][0]["labels"]["alertname"] != "OrdersApiDiskFull":
            return httpx.Response(200)
        disk.append(1)
        return httpx.Response(503 if len(disk) == 1 else 200)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as http:
            watchdog = Watchdog(shop, "http://rootline/incidents", http)
            shop.break_with("disk_full")
            await watchdog.tick()
            await watchdog.tick()
            await watchdog.tick()

    asyncio.run(run())

    assert len(disk) == 2
