from typing import Any

import httpx

from rootline.agent.executor import ShopLite


class FakeShop:
    # Stands in for ShopLite over HTTP: one fault, the action that fixes it, a read-only check.
    def __init__(self, broken: bool = True, fixes: bool = True, detail: str = "") -> None:
        self.broken = broken
        self.fixes = fixes
        self.detail = detail
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/health":
            status = "degraded" if self.broken else "ok"
            body: dict[str, Any] = {
                "status": status,
                "components": {"db": "failing" if self.broken else "ok"},
                "detail": self.detail,
            }
            return httpx.Response(503 if self.broken else 200, json=body)
        if request.url.path == "/admin/actions/raise_pool_limit":
            fixed = ["db_pool_exhausted"] if self.broken else []
            if self.fixes:
                self.broken = False
            body = {"action": "raise_pool_limit", "fixed": fixed, "components": ["db"] * len(fixed)}
            return httpx.Response(200, json=body)
        if request.url.path == "/admin/checks/db_connections":
            return httpx.Response(
                200, json={"check": "db_connections", "output": "count 100 of 100"}
            )
        return httpx.Response(404, json={"detail": "not found"})

    def actions(self) -> list[str]:
        return [call for call in self.calls if "/admin/actions/" in call]

    def client(self) -> ShopLite:
        transport = httpx.MockTransport(self.handle)
        return ShopLite(httpx.Client(transport=transport, base_url="http://shop"))
