""" Tests for the text buffer of the full-screen editor. """
# local
from x84.bbs.textbuffer import (
    TextBuffer, quote_lines, split_paragraphs, wrap_offsets)


def displayed(buf):
    return [buf.row_text(row) for row in buf.rows()]


def type_text(buf, text):
    for ucs in text:
        if ucs == u'\n':
            buf.newline()
        else:
            buf.insert(ucs)


def test_wrap_at_word_boundary():
    text = u'the quick brown fox'
    starts = wrap_offsets(text, 10)
    assert [text[a:b] for a, b in zip(starts, starts[1:] + (len(text),))] == [
        u'the quick ', u'brown fox']


def test_wrap_breaks_long_word():
    assert wrap_offsets(u'abcdefghij', 4) == (0, 4, 8)


def test_wrap_carries_word_then_breaks_it():
    # "x " fits, the long word is carried and then broken.
    assert wrap_offsets(u'x abcdefgh', 4) == (0, 2, 6)


def test_wrap_wide_characters():
    # each CJK character occupies two cells.
    assert wrap_offsets(u'中文字', 4) == (0, 2)


def test_split_paragraphs_any_linebreak():
    assert split_paragraphs(u'a\r\nb\nc\rd') == [u'a', u'b', u'c', u'd']
    assert split_paragraphs(u'') == [u'']
    assert split_paragraphs(u'a\tb') == [u'a       b']


def test_text_roundtrip_crlf():
    buf = TextBuffer(u'one\ntwo\r\n\r\nfour')
    assert buf.text == u'one\r\ntwo\r\n\r\nfour'


def test_typing_wraps_and_joins_paragraph():
    buf = TextBuffer(width=10)
    type_text(buf, u'hello there world\nnext')
    assert buf.paragraphs == [u'hello there world', u'next']
    assert displayed(buf) == [u'hello ', u'there ', u'world', u'next']
    assert buf.cursor_rowcol() == (3, 4)


def test_empty_paragraphs_are_one_row():
    buf = TextBuffer(u'a\r\n\r\nb')
    assert displayed(buf) == [u'a', u'', u'b']


def test_backspace_joins_paragraphs():
    buf = TextBuffer(u'ab\r\ncd')
    buf.down()
    buf.home()
    buf.backspace()
    assert buf.paragraphs == [u'abcd']
    assert (buf.para, buf.pos) == (0, 2)


def test_delete_joins_next_paragraph():
    buf = TextBuffer(u'ab\r\ncd')
    buf.end()
    buf.delete()
    assert buf.paragraphs == [u'abcd']


def test_vertical_movement_keeps_goal_column():
    buf = TextBuffer(u'abcdefgh\r\nab\r\nabcdefgh')
    buf.end()
    assert buf.cursor_rowcol() == (0, 8)
    buf.down()
    assert buf.cursor_rowcol() == (1, 2)
    buf.down()
    assert buf.cursor_rowcol() == (2, 8)


def test_vertical_movement_within_wrapped_paragraph():
    buf = TextBuffer(u'aaaa bbbb cccc', width=5)
    assert displayed(buf) == [u'aaaa ', u'bbbb ', u'cccc']
    buf.down()
    assert (buf.para, buf.pos) == (0, 5)
    buf.end()
    # end of a row that continues stays on that row, after its space.
    assert (buf.pos, buf.cursor_rowcol()) == (9, (1, 4))


def test_up_at_top_and_down_at_bottom():
    buf = TextBuffer(u'abc\r\ndef')
    buf.right()
    buf.up()
    assert (buf.para, buf.pos) == (0, 0)
    buf.down()
    buf.down()
    assert (buf.para, buf.pos) == (1, 3)


def test_left_right_cross_paragraphs():
    buf = TextBuffer(u'a\r\nb')
    buf.right()
    buf.right()
    assert (buf.para, buf.pos) == (1, 0)
    buf.left()
    assert (buf.para, buf.pos) == (0, 1)


def test_overwrite_mode():
    buf = TextBuffer(u'abc')
    buf.overwrite = True
    buf.insert(u'X')
    assert buf.text == u'Xbc'


def test_delete_word_left():
    buf = TextBuffer(u'hello big world')
    buf.end()
    buf.delete_word_left()
    assert buf.text == u'hello big '
    buf.delete_word_left()
    assert buf.text == u'hello '


def test_undo_by_word():
    buf = TextBuffer()
    type_text(buf, u'one two')
    buf.undo()
    assert buf.text == u'one'
    buf.undo()
    assert buf.text == u''
    assert not buf.undo()


def test_undo_restores_cut():
    buf = TextBuffer(u'a\r\nb')
    buf.cut_paragraph()
    assert buf.text == u'b'
    buf.undo()
    assert buf.text == u'a\r\nb'


def test_cut_accumulates_and_paste():
    buf = TextBuffer(u'a\r\nb\r\nc')
    buf.cut_paragraph()
    buf.cut_paragraph()
    assert buf.clipboard == [u'a', u'b']
    assert buf.text == u'c'
    buf.bottom()
    buf.paste()
    assert buf.paragraphs == [u'a', u'b', u'c']


def test_cut_last_paragraph_leaves_empty_document():
    buf = TextBuffer(u'only')
    buf.cut_paragraph()
    assert buf.paragraphs == [u'']


def test_insert_paragraphs_replaces_empty_line():
    buf = TextBuffer()
    buf.insert_paragraphs([u'> quoted'])
    assert buf.paragraphs == [u'> quoted', u'']
    assert (buf.para, buf.pos) == (1, 0)


def test_insert_paragraphs_after_text():
    buf = TextBuffer(u'reply\r\nmore')
    buf.end()
    buf.insert_paragraphs([u'> q'])
    assert buf.paragraphs == [u'reply', u'> q', u'', u'more']
    assert buf.para == 2


def test_max_length():
    buf = TextBuffer(max_length=3)
    type_text(buf, u'abcd')
    assert buf.text == u'abc'
    assert not buf.newline()


def test_quote_lines():
    text = u'hello there world\r\n\r\n> earlier'
    assert quote_lines(text, 12) == [
        u'> hello', u'> there', u'> world', u'>', u'>> earlier']


def test_modified_and_empty():
    buf = TextBuffer()
    assert buf.is_empty and not buf.modified
    buf.insert(u' ')
    assert buf.is_empty and buf.modified
