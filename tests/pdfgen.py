"""A minimal PDF writer for tests: Helvetica text at given positions.

Enough to stand in for a bank statement: one content stream per page, the
standard Helvetica font, text placed left-aligned or right-aligned.
"""

from __future__ import annotations

from pdfminer.fontmetrics import FONT_METRICS

_WIDTHS: dict[str, int] = FONT_METRICS['Helvetica'][1]

Text = tuple[float, float, str]  # x, y (from the bottom), text


def width(text: str, size: float) -> float:
    return sum(_WIDTHS.get(ch, 556) for ch in text) * size / 1000


def right(x1: float, y: float, text: str, size: float = 9) -> Text:
    """Text whose right edge sits at x1, as statements align amounts."""
    return (x1 - width(text, size), y, text)


def _escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def build(pages: list[list[Text]], size: float = 9) -> bytes:
    objects: list[bytes] = []
    n_pages = len(pages)
    # 1 catalog, 2 pages, 3 font, then page and content pairs.
    kids = ' '.join(f'{4 + 2 * i} 0 R' for i in range(n_pages))
    objects.append(b'<< /Type /Catalog /Pages 2 0 R >>')
    objects.append(f'<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>'.encode())
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    for i, texts in enumerate(pages):
        stream = '\n'.join(
            f'BT /F1 {size} Tf {x:.2f} {y:.2f} Td ({_escape(t)}) Tj ET' for x, y, t in texts
        ).encode('latin-1')
        objects.append(
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
            f'/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>'.encode()
        )
        objects.append(b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream')
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % n + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    for off in offsets:
        out += b'%010d 00000 n \n' % off
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)
