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


def test_padding_grows_the_file_to_size_and_keeps_it_a_pdf():
    original = pdf.text_to_pdf('Hello', title='Note')
    data = pdf.pad_pdf(original, 100_000, seed='x')

    assert len(data) == 100_000
    assert data.startswith(original) and data.endswith(b'%%EOF\n')
    assert data == pdf.pad_pdf(original, 100_000, seed='x')

    # The update's xref names the filler object, and its trailer keeps the
    # original's Root and Info and points back at the original xref.
    startxref = int(data.rsplit(b'startxref\n', 1)[1].split(b'\n', 1)[0])
    section = data[startxref:]
    assert section.startswith(b'xref\n')
    first, offset = section.split(b'\n')[1:3]
    assert data[int(offset.split()[0]):].startswith(first.split()[0] + b' 0 obj')
    trailer = section.split(b'trailer\n', 1)[1]
    previous = int(original.rsplit(b'startxref\n', 1)[1].split(b'\n', 1)[0])
    assert b'/Root 1 0 R' in trailer and b'/Info ' in trailer
    assert b'/Prev %d' % previous in trailer


def test_padding_too_small_to_fit_leaves_the_file_alone():
    original = pdf.text_to_pdf('Hello')
    assert pdf.pad_pdf(original, len(original) + 10) == original
