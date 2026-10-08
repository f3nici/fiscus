"""Reading an uploaded file: the size limit, text decoding and CSV rows.

Budget spreadsheets, bill files and payslip PDFs all come through here.
"""

from __future__ import annotations

import csv
import io
from typing import Any

MAX_ROWS = 50_000
MAX_BYTES = 10 * 1024 * 1024


class ImportError_(ValueError):
    """A file that cannot be read, with a message fit for a person."""


def decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ImportError_('That file is larger than 10 MB')
    if b'\x00' in data[:8192]:
        raise ImportError_('The file is not text')
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_('The file is not text')


def read_rows(text: str) -> list[list[str]]:
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if len(rows) > MAX_ROWS:
        raise ImportError_(f'That file has more than {MAX_ROWS:,} rows')
    return rows
