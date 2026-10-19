from fastapi import FastAPI

app = FastAPI(title="rootline")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
