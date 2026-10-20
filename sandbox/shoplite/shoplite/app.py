import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from shoplite.shop import Shop


@dataclass
class Watchdog:
    # Posts an Alertmanager webhook when a fault starts or stops, like a real alert rule.
    shop: Shop
    webhook: str
    client: httpx.AsyncClient
    sent: dict[str, str] = field(default_factory=dict)

    async def tick(self) -> None:
        for fault in self.shop.faults.values():
            firing = fault.name in self.shop.active
            if firing:
                self.shop.log("error", fault.service, fault.error)
            status = "firing" if firing else "resolved"
            if self.sent.get(fault.name, "resolved") == status:
                continue
            try:
                response = await self.client.post(self.webhook, json=self.shop.alert(fault, status))
                response.raise_for_status()
                self.sent[fault.name] = status
            except httpx.HTTPError as error:
                # Tried again on the next tick.
                self.shop.log("warning", "watchdog", f"alert {fault.alert} not delivered: {error}")


def create_app(shop: Shop, webhook: str | None, every: float) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not webhook:
            yield
            return
        async with httpx.AsyncClient(timeout=10) as client:
            watchdog = Watchdog(shop, webhook, client)

            async def watch() -> None:
                while True:
                    await watchdog.tick()
                    await asyncio.sleep(every)

            task = asyncio.create_task(watch())
            yield
            task.cancel()

    app = FastAPI(title="shoplite", lifespan=lifespan)

    @app.get("/health")
    def health() -> JSONResponse:
        report = shop.health()
        return JSONResponse(report, status_code=200 if report["status"] == "ok" else 503)

    @app.get("/orders")
    def orders() -> JSONResponse:
        for name in shop.active:
            fault = shop.faults[name]
            if fault.http_status >= 500:
                return JSONResponse({"error": fault.error}, status_code=fault.http_status)
        return JSONResponse([{"id": 88123, "status": "paid"}, {"id": 88124, "status": "packed"}])

    @app.post("/chaos/{name}")
    def chaos(name: str) -> dict[str, Any]:
        if name not in shop.faults:
            raise HTTPException(404, f"no fault {name}")
        fault = shop.break_with(name)
        return {"fault": name, "alert": fault.alert, "service": fault.service}

    @app.post("/reset")
    def reset() -> dict[str, str]:
        shop.reset()
        return {"status": "ok"}

    @app.post("/admin/actions/{name}")
    def act(name: str) -> dict[str, Any]:
        if name not in shop.actions:
            raise HTTPException(404, f"no action {name}")
        return shop.act(name)

    @app.post("/admin/actions/{name}/dry-run")
    def dry_run(name: str) -> dict[str, Any]:
        if name not in shop.actions:
            raise HTTPException(404, f"no action {name}")
        return shop.dry_run(name)

    @app.get("/admin/checks/{name}")
    def check(name: str) -> dict[str, str]:
        if name not in shop.checks:
            raise HTTPException(404, f"no check {name}")
        return {"check": name, "output": shop.check(name)}

    @app.get("/logs")
    def logs(
        since: datetime | None = None, limit: int = Query(200, le=1000)
    ) -> list[dict[str, str]]:
        lines = [
            line
            for line in shop.logs
            if since is None or datetime.fromisoformat(line["ts"]) >= since
        ]
        return lines[-limit:]

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        lines = [
            f'shoplite_fault_active{{fault="{name}"}} {int(name in shop.active)}'
            for name in shop.faults
        ]
        return PlainTextResponse("\n".join(lines) + "\n")

    return app
