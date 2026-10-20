import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    # The caches are shared by the app containers and local runs. Writing to a temporary file
    # and renaming it means a reader sees the old file or the whole new one, never half of it.
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w") as file:
        file.write(text)
    os.replace(temporary, path)
