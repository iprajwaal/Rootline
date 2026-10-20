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

    assert [payload["status"] for payload in sent] == ["firing", "resolved"]
    [alert] = sent[0]["alerts"]  # type: ignore[misc]
    assert alert["labels"]["alertname"] == "OrdersDbPoolExhausted"
    assert alert["fingerprint"] == sent[1]["alerts"][0]["fingerprint"]  # type: ignore[index]


def test_undelivered_alert_is_sent_again_on_the_next_tick(shop: Shop) -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503 if len(calls) == 1 else 200)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as http:
            watchdog = Watchdog(shop, "http://rootline/incidents", http)
            shop.break_with("disk_full")
            await watchdog.tick()
            await watchdog.tick()
            await watchdog.tick()

    asyncio.run(run())

    assert len(calls) == 2
