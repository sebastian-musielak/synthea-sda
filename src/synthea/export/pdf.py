"""
A minimal plain-text-to-PDF writer.

Clinical notes are plain text, and a consumer that wants a rendered document
(an SDA3 ``Document`` with ``FileType`` PDF, say) needs only pages of that text,
not a layout engine. This writes a PDF 1.4 file using the built-in Courier
font, so it needs no font files and no third-party library, and wrapping is
exact because every glyph is the same width.

The output is a function of the text alone: no creation date, no random file
identifier. Two runs with the same seed produce byte-identical PDFs, which the
exporters' reproducibility guarantee depends on.
"""

import zlib
from typing import List, Optional

PAGE_WIDTH = 612    # US Letter, in points
PAGE_HEIGHT = 792
MARGIN = 54
FONT_SIZE = 10
LEADING = 12

#: Courier glyphs are 0.6 em wide.
_CHAR_WIDTH = FONT_SIZE * 0.6
CHARS_PER_LINE = int((PAGE_WIDTH - 2 * MARGIN) // _CHAR_WIDTH)
LINES_PER_PAGE = int((PAGE_HEIGHT - 2 * MARGIN) // LEADING)


def _wrap(line: str, width: int) -> List[str]:
    """Break one line at spaces into pieces no longer than `width`.

    A word longer than the line is split where it hits the margin. Leading
    indentation is kept on continuation lines so list items stay aligned.
    """
    if len(line) <= width:
        return [line]
    indent = ' ' * min(len(line) - len(line.lstrip(' ')), width // 2)
    pieces: List[str] = []
    rest = line
    while len(rest) > width:
        cut = rest.rfind(' ', 0, width + 1)
        if cut <= len(indent):
            cut = width
        pieces.append(rest[:cut].rstrip())
        rest = indent + rest[cut:].lstrip(' ')
    pieces.append(rest)
    return pieces


def _escape(line: str) -> bytes:
    """A line as the bytes of a PDF literal string, in WinAnsiEncoding.

    Characters the standard fonts cannot draw become ``?`` rather than
    failing the export.
    """
    raw = line.encode('cp1252', errors='replace')
    return (raw.replace(b'\\', b'\\\\')
               .replace(b'(', b'\\(')
               .replace(b')', b'\\)'))


def _pages(text: str) -> List[List[str]]:
    lines: List[str] = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        lines.extend(_wrap(line.expandtabs(4).rstrip(), CHARS_PER_LINE))
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return [[]]
    return [lines[i:i + LINES_PER_PAGE]
            for i in range(0, len(lines), LINES_PER_PAGE)]


def _content(lines: List[str]) -> bytes:
    # The ' operator moves down a line before drawing, so the text origin
    # starts one line above the first baseline.
    first_baseline = PAGE_HEIGHT - MARGIN - FONT_SIZE
    parts = [b'BT', b'/F1 %d Tf' % FONT_SIZE, b'%d TL' % LEADING,
             b'%d %d Td' % (MARGIN, first_baseline + LEADING)]
    for line in lines:
        parts.append(b'(' + _escape(line) + b") '")
    parts.append(b'ET')
    return b'\n'.join(parts)


def text_to_pdf(text: str, title: Optional[str] = None) -> bytes:
    """Render plain text as a PDF, one Courier line per text line."""
    pages = _pages(text)
    objects: List[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b'')          # filled in once the page tree number is known
    page_tree = add(b'')
    font = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier '
               b'/Encoding /WinAnsiEncoding >>')

    kids = []
    for lines in pages:
        stream = zlib.compress(_content(lines), 9)
        content = add(b'<< /Length %d /Filter /FlateDecode >>\nstream\n'
                      % len(stream) + stream + b'\nendstream')
        kids.append(add(
            b'<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] '
            b'/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>'
            % (page_tree, PAGE_WIDTH, PAGE_HEIGHT, font, content)))

    objects[catalog - 1] = b'<< /Type /Catalog /Pages %d 0 R >>' % page_tree
    objects[page_tree - 1] = (
        b'<< /Type /Pages /Kids [' + b' '.join(b'%d 0 R' % k for k in kids)
        + b'] /Count %d >>' % len(kids))

    info = None
    if title:
        info = add(b'<< /Title (' + _escape(title) + b') /Producer (Synthea) >>')

    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'

    xref = len(out)
    out += b'xref\n0 %d\n' % (len(objects) + 1)
    out += b'0000000000 65535 f \n'
    for offset in offsets:
        out += b'%010d 00000 n \n' % offset
    trailer = b'<< /Size %d /Root %d 0 R' % (len(objects) + 1, catalog)
    if info:
        trailer += b' /Info %d 0 R' % info
    out += b'trailer\n' + trailer + b' >>\nstartxref\n%d\n%%%%EOF\n' % xref
    return bytes(out)
