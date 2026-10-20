import os
import re
import subprocess
import sys

import httpx

from rootline.agent.executor import SandboxUnavailable
from rootline.agent.kube import in_cluster
from rootline.catalog import load_catalog
from rootline.config import Settings

QUOTES = re.compile(r"[\"']")
CONTEXT = "kind-shop"


def main() -> None:
    # Runs a command from an SOP. With SHOP=k8s it runs for real against the kind cluster;
    # otherwise the ShopLite sandbox answers it. The shell already removed quotes, so both
    # sides drop them before comparing.
    settings = Settings()
    catalog = load_catalog(settings.catalog_path)
    command = plain(" ".join(sys.argv[1:]))
    checks = {plain(line): name for name, line in catalog.checks.items()}
    actions = {plain(line): name for name, line in catalog.actions.items()}
    name = checks.get(command) or actions.get(command)
    if name is None:
        known = [*catalog.checks.values(), *catalog.actions.values()]
        print("Not an SOP command. These are known:\n  " + "\n  ".join(known))
        sys.exit(2)
    if os.environ.get("SHOP") == "k8s":
        line = catalog.checks.get(name) or catalog.actions[name]
        sys.exit(real(name, line))
    shop = httpx.Client(base_url=settings.shoplite_url, timeout=30)
    if command in checks:
        print(shop.get(f"/admin/checks/{name}").json()["output"])
        return
    done = shop.post(f"/admin/actions/{name}").json()
    print(f"{name}: fixed {', '.join(done['fixed']) or 'nothing'}")
    print(f"health: {shop.get('/health').json()['status']}")


def real(name: str, line: str) -> int:
    if name == "health":
        argv = ["curl", "-s", "localhost:30080/health"]
    else:
        try:
            argv = in_cluster(name, line, CONTEXT)
        except SandboxUnavailable as error:
            print(f"{error}; it is simulated only.")
            return 2
    return subprocess.run(argv, check=False).returncode


def plain(command: str) -> str:
    return " ".join(QUOTES.sub("", command).split())


if __name__ == "__main__":
    main()
