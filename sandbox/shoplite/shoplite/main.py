import os
from pathlib import Path

from shoplite.app import create_app
from shoplite.shop import load_shop

app = create_app(
    load_shop(Path(os.environ.get("SHOPLITE_CATALOG", "catalog.yaml"))),
    os.environ.get("SHOPLITE_ALERT_WEBHOOK"),
    float(os.environ.get("SHOPLITE_WATCH_SECONDS", "5")),
)
