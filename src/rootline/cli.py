import argparse
import uuid
from collections import Counter
from pathlib import Path

import psycopg

from rootline.config import Settings
from rootline.core.context import REQUEST_ID
from rootline.core.logs import configure_logging
from rootline.ingest.factory import build_analyzer, build_store, ingest_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rootline")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingest files or folders")
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--sync", action="store_true", help="delete documents whose file is gone")
    delete = commands.add_parser("delete", help="delete a document by its source key")
    delete.add_argument("source_key")
    args = parser.parse_args(argv)

    REQUEST_ID.set(uuid.uuid4().hex)
    settings = Settings()
    configure_logging(settings.log_level)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        store = build_store(conn, settings, build_analyzer(settings), "cli")
        if args.command == "delete":
            outcome = store.delete(args.source_key)
            print(f"delete source={outcome.source_key} docs={','.join(outcome.doc_ids) or '-'}")
            return
        for path in args.paths:
            outcomes = ingest_path(store, path, sync=args.sync)
            for outcome in outcomes:
                print(
                    f"{outcome.action} source={outcome.source_key} docs={','.join(outcome.doc_ids)}"
                )
            counts = Counter(outcome.action for outcome in outcomes)
            print(f"ingest path={path} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
