import re
import sys

import httpx

from rootline.catalog import load_catalog
from rootline.config import Settings

QUOTES = re.compile(r"[\"']")


def main() -> None:
    # Runs a command from an SOP against the ShopLite sandbox, the way it would run against
    # the real cluster. The shell has already removed the quotes, so both sides drop them.
    settings = Settings()
    catalog = load_catalog(settings.catalog_path)
    command = plain(" ".join(sys.argv[1:]))
    checks = {plain(line): name for name, line in catalog.checks.items()}
    actions = {plain(line): name for name, line in catalog.actions.items()}
    shop = httpx.Client(base_url=settings.shoplite_url, timeout=30)
    if command in checks:
        print(shop.get(f"/admin/checks/{checks[command]}").json()["output"])
    elif command in actions:
        done = shop.post(f"/admin/actions/{actions[command]}").json()
        print(f"{actions[command]}: fixed {', '.join(done['fixed']) or 'nothing'}")
        print(f"health: {shop.get('/health').json()['status']}")
    else:
        known = [*catalog.checks.values(), *catalog.actions.values()]
        print("Not a sandbox command. The sandbox knows:\n  " + "\n  ".join(known))
        sys.exit(2)


def plain(command: str) -> str:
    return " ".join(QUOTES.sub("", command).split())


if __name__ == "__main__":
    main()
