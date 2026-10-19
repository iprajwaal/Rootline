from dataclasses import asdict
from functools import cache
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile

from rootline.config import Settings
from rootline.ingest.analysis import Analyzer
from rootline.ingest.factory import build_analyzer, build_store
from rootline.ingest.parsing import UnsupportedFormat

app = FastAPI(title="rootline")


@cache
def get_settings() -> Settings:
    return Settings()


@cache
def get_analyzer() -> Analyzer:
    # Loaded on first use, so the health check does not wait for the embedding model.
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
            outcome = build_store(conn, settings, analyzer).ingest(key, file.file.read())
        except UnsupportedFormat as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
    return asdict(outcome)


@app.delete("/documents")
def delete_document(
    source_key: str, settings: SettingsDep, analyzer: AnalyzerDep
) -> dict[str, Any]:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        outcome = build_store(conn, settings, analyzer).delete(source_key)
    if not outcome.doc_ids:
        raise HTTPException(status_code=404, detail=f"no document for {source_key}")
    return asdict(outcome)
