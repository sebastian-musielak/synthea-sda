"""Tests for the plain-text PDF writer behind SDA3's PDF note Documents."""

import re
import zlib

from synthea.export import pdf


def _streams(data: bytes):
    return [zlib.decompress(s) for s in
            re.findall(rb'stream\n(.*?)\nendstream', data, re.S)]


def test_output_is_a_well_formed_pdf():
    data = pdf.text_to_pdf('Hello\nWorld', title='Note')

    assert data.startswith(b'%PDF-1.4\n')
    assert data.endswith(b'%%EOF\n')
    startxref = int(data.rsplit(b'startxref\n', 1)[1].split(b'\n', 1)[0])
    assert data[startxref:].startswith(b'xref\n')

    # Every xref offset lands on the object it names.
    table = data[startxref:].split(b'trailer', 1)[0].splitlines()[3:]
    for number, row in enumerate(table, start=1):
        offset = int(row.split()[0])
        assert data[offset:].startswith(b'%d 0 obj' % number)


def test_same_text_gives_the_same_bytes():
    assert pdf.text_to_pdf('a note') == pdf.text_to_pdf('a note')


def test_long_lines_wrap_and_long_notes_paginate():
    text = '\n'.join(['word ' * 40] * pdf.LINES_PER_PAGE)
    data = pdf.text_to_pdf(text)

    assert b'/Count 2' in data or b'/Count 3' in data
    for stream in _streams(data):
        for line in re.findall(rb"\((.*)\) '", stream):
            assert len(line) <= pdf.CHARS_PER_LINE


def test_wrapping_keeps_list_indentation():
    pieces = pdf._wrap('  - ' + 'x ' * 60, 40)

    assert len(pieces) > 1
    assert all(len(p) <= 40 for p in pieces)
    assert all(p.startswith('  ') for p in pieces[1:])


def test_special_and_unencodable_characters_do_not_break_the_file():
    data = pdf.text_to_pdf('a (b) c\\d é 中')
    content = _streams(data)[0]

    assert b'a \\(b\\) c\\\\d \xe9 ?' in content


def test_empty_text_is_still_one_page():
    assert b'/Count 1' in pdf.text_to_pdf('')
