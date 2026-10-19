from pathlib import Path
from typing import assert_never

from rootline.corpus.layout import Bullets, Element, Heading, Layout, Paragraph, Table


def write_markdown(layout: Layout, path: Path) -> None:
    path.write_text(to_markdown(layout))


def to_markdown(layout: Layout) -> str:
    return "\n\n".join(render(element) for element in layout.elements) + "\n"


def render(element: Element) -> str:
    match element:
        case Heading(text, level):
            return f"{'#' * level} {text}"
        case Paragraph(text):
            return text
        case Bullets(items, numbered):
            return "\n".join(
                f"{n}. {item}" if numbered else f"- {item}" for n, item in enumerate(items, start=1)
            )
        case Table(header, rows, code_columns):
            lines = [table_row(header), table_row(["---"] * len(header))]
            lines += [table_row(code_cells(row, code_columns)) for row in rows]
            return "\n".join(lines)
        case _:
            assert_never(element)


def table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def code_cells(row: list[str], code_columns: frozenset[int]) -> list[str]:
    return [f"`{cell}`" if i in code_columns and cell else cell for i, cell in enumerate(row)]
