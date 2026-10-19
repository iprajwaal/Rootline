from pathlib import Path

import psycopg

from rootline.config import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute(
        "create table if not exists schema_migrations ("
        " name text primary key,"
        " applied_at timestamptz not null default now())"
    )
    applied = {row[0] for row in conn.execute("select name from schema_migrations")}

    ran = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
        ran.append(path.name)
    return ran


def main() -> None:
    with psycopg.connect(Settings().database_url, autocommit=True) as conn:
        ran = migrate(conn)
    print(f"migrations applied={len(ran)} files={','.join(ran) or '-'}")


if __name__ == "__main__":
    main()
