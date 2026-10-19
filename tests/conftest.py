from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from rootline.config import Settings

TEST_DB = "rootline_test"


@pytest.fixture
def empty_db() -> Iterator[psycopg.Connection]:
    url = Settings().database_url
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {TEST_DB} with (force)")
        admin.execute(f"create database {TEST_DB}")
    with psycopg.connect(make_conninfo(url, dbname=TEST_DB), autocommit=True) as conn:
        yield conn
