from collections.abc import Callable
from pathlib import Path
from typing import assert_never
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, SimpleDocTemplate, Spacer, TableStyle
from reportlab.platypus import Paragraph as PdfParagraph
from reportlab.platypus import Table as PdfTable
from reportlab.platypus.doctemplate import BaseDocTemplate

from rootline.corpus.layout import COMPANY, Bullets, Element, Heading, Layout, Paragraph, Table

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 20 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
CELL_PADDING = 14.0
MAX_WORD_WIDTH = 45 * mm

STYLES = getSampleStyleSheet()
BODY = STYLES["BodyText"]
BULLET = ParagraphStyle("bullet", parent=BODY, leftIndent=6 * mm)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10)
CELL_BOLD = ParagraphStyle("cell_bold", parent=CELL, fontName="Helvetica-Bold")
CELL_CODE = ParagraphStyle("cell_code", parent=CELL, fontName="Courier")
TABLE_STYLE = TableStyle(
    [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
)

PageDecorator = Callable[[Canvas, BaseDocTemplate], None]


def write_pdf(layout: Layout, path: Path) -> None:
    # Two passes: the first counts pages so the footer can say "Page x of y".
    total_pages = build(layout, path, total_pages=None)
    build(layout, path, total_pages=total_pages)


def build(layout: Layout, path: Path, total_pages: int | None) -> int:
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=30 * mm,
        bottomMargin=20 * mm,
        title=layout.title,
        author=layout.author,
        invariant=True,
    )
    decorate = page_decorator(layout.header_lines, total_pages)
    document.build(flowables(layout), onFirstPage=decorate, onLaterPages=decorate)
    page: int = document.page
    return page


def page_decorator(header_lines: list[str], total_pages: int | None) -> PageDecorator:
    def decorate(canvas: Canvas, document: BaseDocTemplate) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(MARGIN, PAGE_HEIGHT - 15 * mm, COMPANY)
        canvas.setFont("Helvetica", 8)
        for i, line in enumerate(header_lines):
            canvas.drawRightString(PAGE_WIDTH - MARGIN, PAGE_HEIGHT - (13 + 4 * i) * mm, line)
        canvas.line(MARGIN, PAGE_HEIGHT - 23 * mm, PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 23 * mm)
        page = f"Page {document.page} of {total_pages}" if total_pages else f"Page {document.page}"
        canvas.drawString(MARGIN, 12 * mm, "Internal")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, 12 * mm, page)
        canvas.restoreState()

    return decorate


def flowables(layout: Layout) -> list[Flowable]:
    items: list[Flowable] = []
    for element in layout.elements:
        items.extend(to_flowables(element))
    return items


def to_flowables(element: Element) -> list[Flowable]:
    match element:
        case Heading(text, level):
            return [PdfParagraph(escape(text), STYLES[f"Heading{min(level, 4)}"])]
        case Paragraph(text):
            return [PdfParagraph(escape(text), BODY)]
        case Bullets(items, numbered):
            return [
                PdfParagraph(escape(f"{n}. {item}" if numbered else f"• {item}"), BULLET)
                for n, item in enumerate(items, start=1)
            ]
        case Table(header, rows, code_columns):
            return [pdf_table(header, rows, code_columns), Spacer(1, 4 * mm)]
        case _:
            assert_never(element)


def pdf_table(header: list[str], rows: list[list[str]], code_columns: frozenset[int]) -> PdfTable:
    data = [[PdfParagraph(escape(text), CELL_BOLD) for text in header]]
    for row in rows:
        data.append(
            [
                PdfParagraph(escape(text), CELL_CODE if i in code_columns else CELL)
                for i, text in enumerate(row)
            ]
        )
    table = PdfTable(data, colWidths=column_widths(header, rows, code_columns), repeatRows=1)
    table.setStyle(TABLE_STYLE)
    return table


def column_widths(
    header: list[str], rows: list[list[str]], code_columns: frozenset[int]
) -> list[float]:
    # Each column fits its longest word, so tokens like "check:queue_lag" never split.
    # Space left over goes to the columns with the most text.
    minimums = []
    weights = []
    for i, title in enumerate(header):
        cells = [row[i] for row in rows]
        cell_font = CELL_CODE.fontName if i in code_columns else CELL.fontName
        widest = max(
            [word_width(title, CELL_BOLD.fontName)]
            + [word_width(cell, cell_font) for cell in cells]
        )
        minimums.append(min(widest, MAX_WORD_WIDTH) + CELL_PADDING)
        weights.append(min(max(len(text) for text in [title, *cells]), 60))
    if sum(minimums) >= CONTENT_WIDTH:
        return [CONTENT_WIDTH * low / sum(minimums) for low in minimums]
    spare = CONTENT_WIDTH - sum(minimums)
    return [low + spare * w / sum(weights) for low, w in zip(minimums, weights, strict=True)]


def word_width(text: str, font: str) -> float:
    return max((stringWidth(word, font, CELL.fontSize) for word in text.split()), default=0.0)
