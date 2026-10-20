import logging
import os
import time

import httpx

logging.basicConfig(format="%(asctime)s %(levelname)s [traffic] %(message)s", level="INFO")
# Every request would otherwise be logged; only the problems matter here.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("traffic")


def main() -> None:
    # Steady checkout traffic, so a fault shows up in errors and logs as it would in production.
    target = os.environ.get("TARGET", "http://orders-api")
    every = float(os.environ.get("EVERY_SECONDS", "0.5"))
    method, path = os.environ.get("REQUEST", "POST /orders?item=book").split(" ", 1)
    with httpx.Client(base_url=target, timeout=35) as client:
        while True:
            try:
                response = client.request(method, path)
                if response.status_code >= 400:
                    status, body = response.status_code, response.text[:120]
                    log.warning("%s %s %s %s", method, path, status, body)
            except httpx.HTTPError as error:
                log.warning("%s %s failed: %s", method, path, error)
            time.sleep(every)


if __name__ == "__main__":
    main()
