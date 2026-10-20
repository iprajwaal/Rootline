import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from rootline.config import Settings
from rootline.core.context import REQUEST_ID
from rootline.core.logs import configure_logging
from rootline.incidents import (
    AgentUnavailable,
    AlertmanagerWebhook,
    IncidentResult,
    Incidents,
    IncidentView,
    open_chat,
    recent_incidents,
    thread_opener,
)
from rootline.ingest.analysis import Analyzer
from rootline.ingest.factory import build_analyzer, build_store
from rootline.ingest.parsing import UnsupportedFormat
from rootline.ops import ops_report
from rootline.retrieval.documents import Document, read_document, sections
from rootline.retrieval.search import Searcher, SearchResponse
from rootline.retrieval.suggest import Suggester, SuggestResponse
from rootline.review import approve, drafts, fix_type, mark_reviewed, open_items, reject

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    yield


app = FastAPI(title="rootline", lifespan=lifespan)
# The chat UI runs on its own port and calls the alert endpoints from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=Settings().ui_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def tag_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # The caller's ID is kept short because it is stored with every ledger row.
    request_id = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
    token = REQUEST_ID.set(request_id)
    try:
        response = await call_next(request)
    finally:
        REQUEST_ID.reset(token)
    response.headers["x-request-id"] = request_id
    return response


class SuggestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


@cache
def get_settings() -> Settings:
    return Settings()


@cache
def get_analyzer() -> Analyzer:
    # Built on first use, so the health check does not wait for the classifier to train.
    return build_analyzer(get_settings())


SettingsDep = Annotated[Settings, Depends(get_settings)]
AnalyzerDep = Annotated[Analyzer, Depends(get_analyzer)]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/documents")
def upload_document(
    file: UploadFile,
    settings: SettingsDep,
    analyzer: AnalyzerDep,
    source_key: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    key = source_key or f"uploads/{file.filename}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        try:
            outcome = build_store(conn, settings, analyzer, "api").ingest(key, file.file.read())
        except UnsupportedFormat as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
    return asdict(outcome)


@app.delete("/documents")
def delete_document(
    source_key: str, settings: SettingsDep, analyzer: AnalyzerDep
) -> dict[str, Any]:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        outcome = build_store(conn, settings, analyzer, "api").delete(source_key)
    if not outcome.doc_ids:
        raise HTTPException(status_code=404, detail=f"no document for {source_key}")
    return asdict(outcome)


@app.get("/search")
def search(
    q: Annotated[str, Query(min_length=1, max_length=2000)],
    settings: SettingsDep,
    analyzer: AnalyzerDep,
) -> SearchResponse:
    with psycopg.connect(settings.database_url) as conn:
        return Searcher(conn, analyzer.embedder, analyzer.catalog.services).search(q)


@app.get("/docs/{doc_id}", response_model=None)
def read_doc(doc_id: str, request: Request, settings: SettingsDep) -> Document | HTMLResponse:
    with psycopg.connect(settings.database_url) as conn:
        found = read_document(conn, doc_id.upper())
        if found is None:
            raise HTTPException(status_code=404, detail=f"no document {doc_id}")
        # Chat citations open in a browser and get the page; API clients get JSON.
        if "text/html" not in request.headers.get("accept", ""):
            return found
        parts = sections(conn, found)
    return templates.TemplateResponse(request, "doc.html", {"doc": found, "sections": parts})


@app.post("/suggest")
def suggest(
    request: SuggestRequest, settings: SettingsDep, analyzer: AnalyzerDep
) -> SuggestResponse:
    with psycopg.connect(settings.database_url) as conn:
        return suggester(conn, analyzer).suggest(request.text)


@app.post("/incidents")
def receive_alerts(
    webhook: AlertmanagerWebhook, settings: SettingsDep, analyzer: AnalyzerDep
) -> list[IncidentResult]:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        return Incidents(conn, suggester(conn, analyzer)).receive(webhook)


@app.get("/incidents")
def list_incidents(
    settings: SettingsDep,
    status: Literal["open", "all"] = "open",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[IncidentView]:
    with psycopg.connect(settings.database_url) as conn:
        return recent_incidents(conn, status == "all", limit)


@app.post("/incidents/{incident_id}/open")
def open_incident_chat(incident_id: int, settings: SettingsDep) -> dict[str, str]:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        try:
            thread_id = open_chat(conn, incident_id, thread_opener(settings.agent_url))
        except AgentUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    if thread_id is None:
        raise HTTPException(status_code=404, detail=f"no incident {incident_id}")
    return {"thread_id": thread_id}


@app.get("/ops", response_class=HTMLResponse)
def ops(
    request: Request, settings: SettingsDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> HTMLResponse:
    with psycopg.connect(settings.database_url) as conn:
        report = ops_report(conn, days)
    return templates.TemplateResponse(request, "ops.html", {"report": report})


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, settings: SettingsDep) -> HTMLResponse:
    with psycopg.connect(settings.database_url) as conn:
        context = {"drafts": drafts(conn), "items": open_items(conn)}
    return templates.TemplateResponse(request, "review.html", context)


@app.post("/review/{review_id}/approve")
def approve_draft(review_id: int, settings: SettingsDep, analyzer: AnalyzerDep) -> RedirectResponse:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        outcome = approve(conn, build_store(conn, settings, analyzer, "reviewer"), review_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"no open draft {review_id}")
    return RedirectResponse("/review", status_code=303)


@app.post("/review/{review_id}/type")
def set_type(
    review_id: int, doc_type: Annotated[Literal["rca", "sop"], Form()], settings: SettingsDep
) -> RedirectResponse:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        if not fix_type(conn, review_id, doc_type):
            raise HTTPException(status_code=404, detail=f"no open type review {review_id}")
    return RedirectResponse("/review", status_code=303)


@app.post("/review/{review_id}/done")
def review_done(review_id: int, settings: SettingsDep) -> RedirectResponse:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        if not mark_reviewed(conn, review_id):
            raise HTTPException(status_code=404, detail=f"no open review item {review_id}")
    return RedirectResponse("/review", status_code=303)


@app.post("/review/{review_id}/reject")
def reject_draft(review_id: int, settings: SettingsDep) -> RedirectResponse:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        if not reject(conn, review_id):
            raise HTTPException(status_code=404, detail=f"no open draft {review_id}")
    return RedirectResponse("/review", status_code=303)


def suggester(conn: psycopg.Connection[Any], analyzer: Analyzer) -> Suggester:
    searcher = Searcher(conn, analyzer.embedder, analyzer.catalog.services)
    return Suggester(searcher, analyzer.catalog.alerts)
