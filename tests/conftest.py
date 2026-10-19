from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from rootline.config import Settings
from rootline.db.migrate import migrate
from rootline.ingest.analysis import Analyzer
from rootline.ingest.factory import build_analyzer
from rootline.ingest.store import Store

TEST_DB = "rootline_test"
SEARCH_DB = "rootline_search_test"
CORPUS = Path(__file__).parents[1] / "corpus"


def isolated_url() -> str:
    return make_conninfo(Settings().database_url, dbname=TEST_DB)


def search_url() -> str:
    return make_conninfo(Settings().database_url, dbname=SEARCH_DB)


@pytest.fixture(scope="session")
def search_db_url() -> str:
    return search_url()


@pytest.fixture(scope="session")
def db_url() -> str:
    return isolated_url()


@pytest.fixture
def empty_db() -> Iterator[psycopg.Connection]:
    url = Settings().database_url
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {TEST_DB} with (force)")
        admin.execute(f"create database {TEST_DB}")
    with psycopg.connect(isolated_url(), autocommit=True) as conn:
        yield conn


@pytest.fixture
def migrated_db(empty_db: psycopg.Connection) -> psycopg.Connection:
    migrate(empty_db)
    return empty_db


@pytest.fixture(scope="session")
def offline_settings() -> Settings:
    # No API key: LLM answers come only from the committed cache, so tests never call out.
    return Settings(database_url=isolated_url(), google_api_key="")


@pytest.fixture(scope="session")
def analyzer(offline_settings: Settings) -> Analyzer:
    return build_analyzer(offline_settings)


@pytest.fixture
def store(
    migrated_db: psycopg.Connection, analyzer: Analyzer, offline_settings: Settings, tmp_path: Path
) -> Store:
    return Store(migrated_db, analyzer, tmp_path / "raw", offline_settings.embed_model)


@pytest.fixture(scope="session")
def search_db(
    analyzer: Analyzer, offline_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[psycopg.Connection]:
    # Search tests read one shared index of the greenfield and brownfield corpus.
    url = Settings().database_url
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {SEARCH_DB} with (force)")
        admin.execute(f"create database {SEARCH_DB}")
    with psycopg.connect(search_url(), autocommit=True) as conn:
        migrate(conn)
        store = Store(conn, analyzer, tmp_path_factory.mktemp("raw"), offline_settings.embed_model)
        for folder in ("greenfield", "brownfield"):
            for path in sorted((CORPUS / folder).iterdir()):
                store.ingest(f"corpus/{folder}/{path.name}", path.read_bytes())
        yield conn
