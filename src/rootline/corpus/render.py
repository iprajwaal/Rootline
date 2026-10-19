from collections.abc import Callable
from pathlib import Path

from rootline.corpus.docx_writer import write_docx
from rootline.corpus.layout import Layout, layout_for, load_source
from rootline.corpus.md_writer import write_markdown
from rootline.corpus.pdf_writer import write_pdf

SOURCE_DIR = Path("corpus/sources/greenfield")
OUTPUT_DIR = Path("corpus/greenfield")
WRITERS: dict[str, Callable[[Layout, Path], None]] = {
    "md": write_markdown,
    "docx": write_docx,
    "pdf": write_pdf,
}


def render_all(source_dir: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for path in sorted(source_dir.glob("*.yaml")):
        source = load_source(path)
        target = output_dir / f"{path.stem}.{source.render_as}"
        WRITERS[source.render_as](layout_for(source), target)
        written.append(target)
    return written


def main() -> None:
    written = render_all(SOURCE_DIR, OUTPUT_DIR)
    print(f"rendered files={len(written)} dir={OUTPUT_DIR}")


if __name__ == "__main__":
    main()
