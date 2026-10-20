import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

QUERIES = Path("eval/queries/queries.jsonl")
SEED = 7
TEST_SHARE = 0.3


def main() -> None:
    # Split within each kind, so dev and test hold the same mix of requests.
    by_kind: dict[str, list[str]] = defaultdict(list)
    for line in QUERIES.read_text().splitlines():
        by_kind[json.loads(line)["kind"]].append(line)
    rng = random.Random(SEED)
    dev, test = [], []
    for kind in sorted(by_kind):
        lines = by_kind[kind]
        rng.shuffle(lines)
        cut = round(len(lines) * TEST_SHARE)
        test += lines[:cut]
        dev += lines[cut:]
    for name, lines in (("dev", dev), ("test", test)):
        (QUERIES.parent / f"{name}.jsonl").write_text("".join(f"{line}\n" for line in lines))
    # Recorded so a later change to the held-out set shows up in review.
    digest = hashlib.sha256((QUERIES.parent / "test.jsonl").read_bytes()).hexdigest()
    (QUERIES.parent / "test.sha256").write_text(f"{digest}  test.jsonl\n")
    print(f"dev={len(dev)} test={len(test)} test sha256={digest[:16]}")


if __name__ == "__main__":
    main()
