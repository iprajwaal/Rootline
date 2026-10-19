import hashlib
import os
import uuid
from pathlib import Path

from rootline.models import ParsedDoc


def raw_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(doc: ParsedDoc) -> str:
    return hashlib.sha256(doc.text.encode()).hexdigest()


def store_raw(data: bytes, root: Path) -> str:
    digest = raw_sha256(data)
    target = root / digest
    if not target.exists():
        root.mkdir(parents=True, exist_ok=True)
        # Write to a unique temp file, then rename. The rename is atomic, so two writers of the
        # same file can race safely and a reader never sees a half-written file.
        temp = root / f".{digest}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        temp.write_bytes(data)
        os.replace(temp, target)
    return digest
