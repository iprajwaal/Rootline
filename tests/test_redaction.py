import json
from pathlib import Path
from typing import Any

import pytest

from rootline.ingest.parsing import parse_file
from rootline.ingest.redaction import Redaction, redact, redact_doc

CORPUS = Path(__file__).parents[1] / "corpus"
TRUTH: list[dict[str, Any]] = [
    json.loads(line)
    for line in (CORPUS / "ground_truth" / "brownfield.jsonl").read_text().splitlines()
]
CLEAN_FILES = sorted(
    [*(CORPUS / "greenfield").iterdir(), *(CORPUS / "reference").iterdir()]
    + [CORPUS / row["file"] for row in TRUTH if not row["canaries"]]
)

# Provider-style fakes are joined at runtime so this file never holds a string that a repo
# secret scanner would flag.
SECRETS = [
    ("password=Sup3r-Secret!", "PASSWORD"),
    ("psql login: user shop, password Canary-Db-Pass-7731", "PASSWORD"),
    ("postgresql://shop:hunter22x@db:5432/orders", "PASSWORD"),
    ("AK" + "IA" + "Q3EXAMPLE7KEY9ID", "AWS_ACCESS_KEY"),
    ("AI" + "za" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q", "GOOGLE_API_KEY"),
    ("gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "GITHUB_TOKEN"),
    ("xo" + "xb-" + "1234567890-abcdefghij", "SLACK_TOKEN"),
    ("new key is pk_canary_5d2e9b7f1a4c8e3b", "API_KEY"),
    ("Authorization: Bearer abcdef0123456789abcdef", "TOKEN"),
    ("https://pay.example.com/v2?token=canary-tok-44f1e9", "TOKEN"),
    ("api_key: 9f8e7d6c5b4a", "SECRET"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.Y2FuYXJ5LXNpZw", "JWT"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "PRIVATE_KEY"),
    ("the primary db is at 10.20.30.40", "PRIVATE_IP"),
    ("ssh to 192.168.4.12 first", "PRIVATE_IP"),
    ("direct host is pg-primary.shop.internal", "INTERNAL_HOST"),
    ("session id Zx9Qw3Er7Ty2Ui8Op4As6Df1", "HIGH_ENTROPY"),
]

ORDINARY_OPS_TEXT = [
    "kubectl -n shop set env deploy/orders-api DB_POOL_MAX=50",
    "curl -s http://orders-api:8080/health",
    "kafka-consumer-groups --bootstrap-server order-queue:9092 --describe --group orders-worker",
    "Rolled orders-worker back from 2.7 to 2.6, then released 2.7.1.",
    "Never paste passwords into chat. Reset the password through the admin page.",
    "redis.exceptions.ConnectionError: Error 111 connecting to cache:6379.",
    "inc-2025-06-03-orders-api-outage-from-exhausted-pool",
    "Rotate the token when it expires.",
]


@pytest.mark.parametrize(("text", "kind"), SECRETS, ids=[kind for _, kind in SECRETS])
def test_secret_is_replaced_with_typed_placeholder(text: str, kind: str) -> None:
    result = redact(text)

    assert result.kinds == [kind]
    assert f"[REDACTED:{kind}]" in result.text


@pytest.mark.parametrize("text", ORDINARY_OPS_TEXT)
def test_ordinary_ops_text_is_left_alone(text: str) -> None:
    result = redact(text)

    assert result.text == text
    assert result.kinds == []


def test_redaction_is_idempotent() -> None:
    once = redact(" ".join(text for text, _ in SECRETS)).text

    assert redact(once).text == once


@pytest.mark.parametrize("row", [r for r in TRUTH if r["canaries"]], ids=lambda r: r["file"])
def test_no_planted_canary_survives_parsing_and_redaction(row: dict[str, Any]) -> None:
    doc, kinds = redact_doc(parse_file(CORPUS / row["file"]))
    everything = doc.model_dump_json()

    assert kinds
    for value in row["canaries"]:
        assert value not in everything


@pytest.mark.parametrize("path", CLEAN_FILES, ids=lambda path: path.name)
def test_clean_documents_have_no_findings(path: Path) -> None:
    _, kinds = redact_doc(parse_file(path))

    assert kinds == []


def test_redacted_doc_offsets_still_match() -> None:
    doc, _ = redact_doc(parse_file(CORPUS / "brownfield" / "re-orders-api-503s-this-morning.eml"))

    for block in doc.blocks:
        assert doc.text[block.start : block.end] == block.text


def test_detect_secrets_catches_a_quoted_secret_our_rules_miss() -> None:
    result = redact('db_password: "s3cr3t-Value!"')

    assert result.text == 'db_password: "[REDACTED:SECRET_KEYWORD]"'
    assert result.kinds == ["SECRET_KEYWORD"]


@pytest.mark.parametrize(
    "line",
    [
        "restart the cache with kubectl rollout restart statefulset/cache",
        "set DB_POOL_MAX=50 and check the password reset page",
        'psql -h orders-db -U shop -c "select count(*) from pg_stat_activity"',
    ],
)
def test_detect_secrets_leaves_ordinary_ops_lines_alone(line: str) -> None:
    assert redact(line) == Redaction(line, [])
