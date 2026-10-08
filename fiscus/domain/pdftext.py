"""Text out of a PDF, with where each character sits.

Characters are grouped into lines by baseline and into cells by the gaps
between them. The payslip reader works from those lines and cells. Parsing
bytes is the only work; no I/O.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fiscus.domain.uploads import ImportError_

MAX_PAGES = 100


@dataclass(frozen=True)
class Char:
    text: str
    x0: float
    x1: float
    y: float
    size: float


@dataclass
class Cell:
    text: str
    x0: float
    x1: float


@dataclass
class Line:
    page: int
    y: float
    size: float
    cells: list[Cell]


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b'%PDF-')


# ---------------------------------------------------------------- extraction


def extract_chars(data: bytes, max_pages: int = MAX_PAGES) -> list[list[Char]]:
    """Every visible character, page by page."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar
    from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect

    def walk(obj: Any) -> Iterator[Any]:
        if isinstance(obj, LTChar):
            yield obj
            return
        try:
            children = list(obj)
        except TypeError:
            return
        for child in children:
            yield from walk(child)

    pages: list[list[Char]] = []
    try:
        for page in extract_pages(io.BytesIO(data), maxpages=max_pages, laparams=None):
            chars = [
                Char(c.get_text(), c.x0, c.x1, round(c.y0, 1), c.size or 1.0)
                for c in walk(page)
                if c.get_text().strip()
            ]
            pages.append(chars)
    except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
        raise ImportError_(
            'That PDF is password protected. Save an unlocked copy and upload that.'
        ) from exc
    except ImportError_:
        raise
    except Exception as exc:  # pdfminer raises many types for a damaged file
        raise ImportError_('That PDF could not be read') from exc
    return pages


def lines_from_chars(pages: list[list[Char]]) -> list[Line]:
    out: list[Line] = []
    for pno, chars in enumerate(pages):
        rows: list[list[Char]] = []
        for c in sorted(chars, key=lambda c: (-c.y, c.x0)):
            if rows and abs(rows[-1][0].y - c.y) <= max(2.0, c.size * 0.3):
                rows[-1].append(c)
            else:
                rows.append([c])
        for row in rows:
            row.sort(key=lambda c: c.x0)
            size = max(c.size for c in row)
            cells: list[Cell] = []
            prev: Char | None = None
            for c in row:
                gap = c.x0 - prev.x1 if prev else 0.0
                if prev is None or gap > size * 0.8:
                    cells.append(Cell(c.text, c.x0, c.x1))
                else:
                    cell = cells[-1]
                    cell.text += (' ' if gap > size * 0.15 else '') + c.text
                    cell.x1 = c.x1
                prev = c
            out.append(Line(pno, row[0].y, size, cells))
    return out
