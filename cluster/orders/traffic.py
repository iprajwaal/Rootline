import logging
import os
import time

import httpx

logging.basicConfig(format="%(asctime)s %(levelname)s [traffic] %(message)s", level="INFO")
log = logging.getLogger("traffic")


def main() -> None:
    # Steady checkout traffic, so a fault shows up in errors and logs as it would in production.
    target = os.environ.get("TARGET", "http://orders-api")
    every = float(os.environ.get("EVERY_SECONDS", "0.5"))
    with httpx.Client(base_url=target, timeout=35) as client:
        while True:
            try:
                response = client.post("/orders", params={"item": "book"})
                if response.status_code >= 400:
                    log.warning("POST /orders %s %s", response.status_code, response.text[:120])
            except httpx.HTTPError as error:
                log.warning("POST /orders failed: %s", error)
            time.sleep(every)


if __name__ == "__main__":
    main()
