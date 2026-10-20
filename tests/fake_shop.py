from typing import Any

import httpx

from rootline.agent.executor import ShopLite


class FakeShop:
    # Stands in for ShopLite over HTTP: one fault, the action that fixes it, a read-only check.
    def __init__(
        self,
        broken: bool = True,
        fixes: bool = True,
        detail: str = "",
        component: str = "db",
        outputs: dict[str, str] | None = None,
        fault: str = "db_pool_exhausted",
        fix: str = "raise_pool_limit",
    ) -> None:
        self.broken = broken
        self.fixes = fixes
        self.detail = detail
        self.component = component
        self.fault = fault
        self.fix = fix
        self.outputs = outputs or {"db_connections": "count 100 of 100"}
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/health":
            return httpx.Response(503 if self.broken else 200, json=self.health(self.broken))
        if path.startswith("/admin/actions/"):
            name = path.removeprefix("/admin/actions/").removesuffix("/dry-run")
            fixed = [self.fault] if self.broken and name == self.fix else []
            body = {"action": name, "fixed": fixed, "components": [self.component] * len(fixed)}
            if path.endswith("/dry-run"):
                after = self.broken and not (fixed and self.fixes)
                return httpx.Response(200, json=body | {"health": self.health(after)})
            if fixed and self.fixes:
                self.broken = False
            return httpx.Response(200, json=body)
        name = request.url.path.removeprefix("/admin/checks/")
        if name in self.outputs:
            return httpx.Response(200, json={"check": name, "output": self.outputs[name]})
        return httpx.Response(404, json={"detail": "not found"})

    def health(self, broken: bool) -> dict[str, Any]:
        return {
            "status": "degraded" if broken else "ok",
            "components": {self.component: "failing" if broken else "ok"},
            "detail": self.detail,
        }

    def actions(self) -> list[str]:
        # Only real runs: a dry run changes nothing.
        return [c for c in self.calls if "/admin/actions/" in c and not c.endswith("/dry-run")]

    def client(self) -> ShopLite:
        transport = httpx.MockTransport(self.handle)
        return ShopLite(httpx.Client(transport=transport, base_url="http://shop"))
