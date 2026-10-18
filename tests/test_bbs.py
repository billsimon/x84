""" Tests for the scripting library (x84.bbs) of x/84. """
# std imports
import codecs

# 3rd-party
import pytest
from blessed.keyboard import Keystroke


def key(term, ucs, code=None):
    return Keystroke(ucs, code=code)


# -- widgets

def test_pager_scrolls(session):
    """ Pager.bottom regression: pager previously could not scroll at all. """
    from x84.bbs import Pager
    pager = Pager(height=5, width=40, yloc=0, xloc=0)
    pager.update(u'\r\n'.join(u'line {0}'.format(n) for n in range(20)))
    assert pager.visible_height == 3
    assert pager.bottom == 17
    pager.move_down()
    assert pager.position == 1
    pager.move_end()
    assert pager.position == 17
    assert pager.visible_content[-1] == u'line 19'
    pager.move_pgup()
    assert pager.position == 14
    pager.move_home()
    assert pager.position == 0


def test_keysets_are_not_shared(session):
    """ Each instance appended to the global, shared keyset lists. """
    from x84.bbs import LineEditor, Lightbar, Pager
    from x84.bbs.editor import PC_KEYSET
    from x84.bbs.lightbar import NETHACK_KEYSET
    before = {name: len(keys) for name, keys in PC_KEYSET.items()}
    for _ in range(3):
        LineEditor(10)
        Lightbar(height=5, width=20, yloc=0, xloc=0)
        Pager(height=5, width=20, yloc=0, xloc=0)
    assert {name: len(keys) for name, keys in PC_KEYSET.items()} == before
    assert len(NETHACK_KEYSET['exit']) == 3


def test_line_editor(session):
    from x84.bbs import LineEditor
    term = session.term
    editor = LineEditor(width=5)
    for char in u'hello world':
        editor.process_keystroke(key(term, char))
    assert editor.content == u'hello'
    editor.process_keystroke(key(term, u'\x7f', term.KEY_BACKSPACE))
    assert editor.content == u'hell'
    editor.process_keystroke(key(term, u'\r', term.KEY_ENTER))
    assert editor.carriage_returned


def test_line_editor_unknown_sequence(session):
    """ ESC ESC yields a sequence with code None, which raised TypeError. """
    from x84.bbs import LineEditor
    term = session.term
    editor = LineEditor(width=5)
    assert editor.process_keystroke(None) == u''
    assert editor.process_keystroke(key(term, u'\x1b\x1b')) == u''
    assert editor.content == u''


def test_line_editor_unlimited_width(session):
    """ width=None, the default, previously raised TypeError on input. """
    from x84.bbs import LineEditor
    editor = LineEditor()
    editor.process_keystroke(key(session.term, u'x'))
    assert editor.content == u'x'


def test_line_editor_read(session):
    from x84.bbs import LineEditor
    session.keys(u'jojo\r')
    assert LineEditor(10).read() == u'jojo'
    session.keys(u'abc\x1b')
    assert LineEditor(10).read() is None


def test_lightbar(session):
    from x84.bbs import Lightbar
    term = session.term
    lightbar = Lightbar(height=4, width=20, yloc=0, xloc=0)
    lightbar.update([(idx, u'item {0}'.format(idx)) for idx in range(10)])
    assert lightbar.selection == (0, u'item 0')
    lightbar.process_keystroke(key(term, u'j'))
    lightbar.process_keystroke(key(term, u'\x1b[B', term.KEY_DOWN))
    assert lightbar.selection == (2, u'item 2')
    lightbar.process_keystroke(key(term, u'G'))
    assert lightbar.selection == (9, u'item 9')
    lightbar.process_keystroke(key(term, u'\r', term.KEY_ENTER))
    assert lightbar.selected


def test_selector(session):
    from x84.bbs import Selector
    term = session.term
    sel = Selector(yloc=0, xloc=0, width=20, left=u'Yes', right=u'No')
    assert sel.selection == u'Yes'
    sel.process_keystroke(key(term, u'\x1b[C', term.KEY_RIGHT))
    assert sel.selection == u'No'
    sel.process_keystroke(key(term, u' '))
    assert sel.selection == u'Yes'


def test_ansiwindow_title_and_fit(session):
    from x84.bbs import AnsiWindow
    outer = AnsiWindow(height=10, width=40, yloc=0, xloc=0)
    inner = AnsiWindow(height=5, width=20, yloc=2, xloc=2)
    assert outer.willfit(inner)
    assert not inner.willfit(outer)
    # integer division: a float cursor position would be invalid.
    assert u'.' not in outer.title(u'title')
    assert outer.border()


def test_scrolling_editor(session):
    from x84.bbs import ScrollingEditor
    term = session.term
    editor = ScrollingEditor(width=20, yloc=0, xloc=0, max_length=30)
    for char in u'some text':
        editor.process_keystroke(key(term, char))
    assert editor.content == u'some text'
    editor.process_keystroke(key(term, u'\x17'))
    assert editor.content == u'some '


# -- output

def test_echo_and_decode_pipe(session):
    from x84.bbs import echo, decode_pipe, encode_pipe
    term = session.term
    assert decode_pipe(u'no pipes') == u'no pipes'
    # pipe codes are of ANSI color order, matching encode_pipe().
    decoded = decode_pipe(u'|01red|07 normal ||literal')
    assert term.red in decoded and u'|literal' in decoded
    assert encode_pipe(u'\x1b[31mred') == u'|01red'
    with pytest.warns(UnicodeWarning):
        echo(b'bytes')
    assert session.output() == u'bytes'


def test_timeago():
    from x84.bbs import timeago
    assert timeago(126.32).strip() == u'2m 6s'
    assert timeago(90061).strip() == u'1d 1h'


def test_syncterm_setfont():
    from x84.bbs import syncterm_setfont
    assert syncterm_setfont('topaz') == u'\x1b[0;40 D'
    with pytest.raises(ValueError):
        syncterm_setfont('no-such-font')


def test_showart_sauce_and_encoding(session, tmp_path):
    from x84.bbs import showart
    from sauce import SAUCE
    artfile = tmp_path / 'art.ans'
    artfile.write_bytes(b'\xdb\xdb hello\r\n\xb0\xb1\xb2\r\n\x1a')
    record = SAUCE(data=artfile.read_bytes())
    record.title = 'test'
    record.write(str(artfile))
    lines = list(showart(str(artfile), encoding='cp437_art'))
    text = u''.join(lines)
    assert u'██ hello' in text
    assert u'░▒▓' in text
    assert u'SAUCE' not in text


def test_showart_missing(session):
    from x84.bbs import showart
    assert u'no files matching' in u''.join(showart('/no/such/*.ans'))


# -- encodings

@pytest.mark.parametrize('name', ['cp437_art', 'cp437art', 'amiga',
                                  'topaz', 'atarist', 'atari', 'msdos'])
def test_codecs_registered(name):
    import x84.encodings  # noqa
    codecs.lookup(name)
    alnum = b'0123456789abcdefghijklmnopqrstuvwxyz'
    assert alnum.decode(name) == alnum.decode('ascii')


def test_cp437_art_glyphs():
    import x84.encodings  # noqa
    assert b'\x01\x02\x03'.decode('cp437_art') == u'☺☻♥'
    decoder = codecs.getincrementaldecoder('cp437_art')()
    assert decoder.decode(b'\xdb') == u'█'


ALL_BYTES = bytes(range(256))


def test_cp437_art_round_trip():
    # each byte, such as those of a cp437 door, is sent unchanged to a
    # cp437 terminal; ▲ and ▼ could not be encoded.
    from x84.encodings import encode_output
    text = ALL_BYTES.decode('cp437_art')
    assert text.encode('cp437_art') == ALL_BYTES
    assert encode_output(text, 'cp437') == ALL_BYTES
    assert codecs.getincrementalencoder('cp437_art')().encode(
        text) == ALL_BYTES
    # so is text decoded as plain cp437, of control characters.
    assert encode_output(ALL_BYTES.decode('cp437'), 'cp437') == ALL_BYTES


def test_cp437_art_encoding_table_agrees_with_map():
    # the fast encoding table, and the map of its fallback, must agree.
    from x84.encodings.cp437_art import DECODING_TABLE, ENCODING_MAP
    for byte, ucs in enumerate(DECODING_TABLE):
        assert ENCODING_MAP[ord(ucs)] == byte, hex(byte)


def test_cp437_art_glyph_bytes():
    assert u'▲▼⌂→'.encode('cp437_art') == b'\x1e\x1f\x7f\x1a'
    assert b'\x7f'.decode('cp437_art') == u'⌂'
    # BULLET was encoded as BEL, which rings the terminal's bell.
    with pytest.raises(UnicodeEncodeError):
        u'•'.encode('cp437_art')


@pytest.mark.parametrize('ucs,expected', [
    (u'café', b'caf\x82'),
    (u'café', b'caf\x82'),
    (u'naïve ã', b'na\x8bve a'),
    (u'‘quote’ “quote”', b"'quote' \"quote\""),
    (u'a—b–c', b'a-b-c'),
    (u'• item', b'\xf9 item'),
    (u'← back', b'< back'),
    (u'╭━╮', b'\xda\xc4\xbf'),
    (u'✓', b'\xfb'),
    (u'中|', b'??|'),
    (u'Ａ|', b'A |'),
    (u'\U0001f600|', b'??|'),
    (u'a‍b\x85c', b'abc'),
])
def test_encode_output_cp437(ucs, expected):
    from x84.encodings import encode_output
    assert encode_output(ucs, 'cp437') == expected


@pytest.mark.parametrize('ucs', [
    u'café café', u'中文', u'ＡＢ', u'—',
    u'\U0001f600', u'╭━╮', u'…', u'™', u'ﬁ',
])
def test_encode_output_cp437_keeps_width(ucs):
    # what a cp437 terminal draws fills as many cells as on a utf-8 one.
    import wcwidth
    from x84.encodings import encode_output
    drawn = encode_output(ucs, 'cp437').decode('cp437_art')
    assert wcwidth.wcswidth(drawn) == wcwidth.wcswidth(ucs)


def test_encode_output_utf8():
    from x84.encodings import encode_output
    text = ALL_BYTES.decode('cp437_art') + u'中\U0001f600'
    assert encode_output(text, 'utf8') == text.encode('utf8')
    assert encode_output(u'\ud800x', 'utf8') == b'?x'


@pytest.mark.parametrize('name,expected', [
    ('utf8', 'utf8'), ('UTF-8', 'utf8'), ('utf_8', 'utf8'),
    ('cp437', 'cp437'), ('IBM437', 'cp437'), ('msdos', 'cp437'),
    ('no-such-codec', 'utf8'),
])
def test_session_encoding(name, expected):
    from x84.encodings import session_encoding
    assert session_encoding(name) == expected


def test_sauce_font_codecs():
    from x84.bbs.output import SAUCE_FONT_MAP
    assert SAUCE_FONT_MAP['IBM VGA25G 437'] == 'cp437'
    for codec in set(SAUCE_FONT_MAP.values()):
        codecs.lookup(codec)


def test_split_art_lines():
    from x84.bbs.output import split_art_lines
    # only LF and CRLF end a line, not the glyphs ▲ ∟ ♀ ♂, or a bare CR.
    assert split_art_lines(u'a\x1eb\x1c\x0c\x0b\rc\r\nd\n') == [
        u'a\x1eb\x1c\x0c\x0b\rc', u'd', u'']


def test_showart_unknown_encoding(session, tmp_path):
    from x84.bbs import showart
    artfile = tmp_path / 'art.ans'
    artfile.write_bytes(b'\x01\xdb\r\n')
    assert u'☺█' in u''.join(showart(str(artfile), encoding='cp872'))


@pytest.mark.parametrize('encoding', [None, 'cp437', 'cp437_art'])
def test_showart_same_for_cp437_and_utf8(session, tmp_path, encoding):
    # a cp437 terminal draws the bytes it is sent as cp437 art; a utf-8
    # terminal must be sent the same glyphs.
    from x84.bbs import showart
    from x84.encodings import encode_output
    art = (b'\x1b[1;37m\x01\x02 \x0b\x0c \x18\x19\x1a\x1b[0m\r\n'
           b'\x1e\x1f\x1c\x1d\x7f \xdb\xb0\xb1\xb2 \xc9\xcd\xbb\r\n'
           b'\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\r\n')
    artfile = tmp_path / 'art.ans'
    artfile.write_bytes(art)
    drawn = {}
    for term_encoding in ('utf8', 'cp437'):
        session.session.encoding = term_encoding
        text = u''.join(showart(str(artfile), encoding=encoding))
        sent = encode_output(text, term_encoding)
        drawn[term_encoding] = sent.decode(
            'utf8' if term_encoding == 'utf8' else 'cp437_art')
        if term_encoding == 'cp437':
            for line in art.split(b'\r\n')[:-1]:
                assert line + b'\r\n' in sent
    assert drawn['utf8'] == drawn['cp437']
    assert u'☺☻ ♂♀ ↑↓' in drawn['utf8']
    assert u'▲▼∟↔⌂' in drawn['utf8']


# -- message base

def test_msgbase(session):
    from tests.test_session import MiniEngine
    from x84.bbs import Msg, get_msg, list_msgs, list_tags, list_privmsgs
    engine = MiniEngine(session)
    engine.start()
    try:
        first = Msg(subject=u'hello', body=u'world')
        first.tags = {u'public', u'python'}
        first.save()
        assert first.idx == 0
        reply = Msg(recipient=u'anonymous', subject=u're: hello', body=u'hi')
        reply.parent = first.idx
        reply.tags = {u'public'}
        reply.save()
        assert reply.idx == 1
        private = Msg(recipient=u'jojo', subject=u'secret', body=u'psst')
        private.save()
        assert get_msg(0).children == {1}
        assert list_msgs() == {0, 1, 2}
        assert list_msgs(tags=(u'python',)) == {0}
        assert sorted(list_tags()) == [u'public', u'python']
        assert list_privmsgs(u'jojo') == {2}
        assert get_msg(1).stime is not None
    finally:
        engine.stopped = True
        engine.join()


def test_msgbase_time_conversion():
    import datetime
    from x84.bbs.msgbase import to_utctime, to_localtime
    when = datetime.datetime(2020, 1, 2, 3, 4, 5)
    assert to_localtime(to_utctime(when)) == when


# -- configuration

def test_get_ini(cfg):
    from x84.bbs import get_ini
    assert get_ini('system', 'bbsname') == 'x/84'
    assert get_ini('nua', 'max_user', getter='getint') == 11
    assert get_ini('matrix', 'byecmds', split=True) == [
        'exit', 'logoff', 'bye', 'quit']
    assert get_ini('missing', 'option') == ''
    assert get_ini('missing', 'option', split=True) == []
    assert get_ini('missing', 'option', getter='getboolean') is False


def test_ini_init_creates_defaults(tmp_path, monkeypatch):
    import x84.bbs.ini
    monkeypatch.setattr(x84.bbs.ini, 'CFG', None)
    bbs_ini, log_ini = tmp_path / 'x84' / 'default.ini', tmp_path / 'log.ini'
    x84.bbs.ini.init((str(bbs_ini),), (str(log_ini),))
    assert bbs_ini.exists() and log_ini.exists()
    assert x84.bbs.ini.CFG.get('telnet', 'port') == '6023'
    # the daily log is placed beside the logging configuration file.
    assert str(tmp_path / 'daily.log') in log_ini.read_text()


def test_cmdline():
    from x84.cmdline import parse_args
    lookup_bbs, lookup_log = parse_args(['--config', '/tmp/a.ini'])
    assert lookup_bbs == ('/tmp/a.ini',)
    assert lookup_log[-1].endswith('logging.ini')
    with pytest.raises(SystemExit):
        parse_args(['--bogus'])


def test_server_enabled(cfg):
    from x84.engine import server_enabled
    assert server_enabled(cfg, 'telnet')
    assert not server_enabled(cfg, 'rlogin')
    cfg.remove_section('ssh')
    # previously raised NoSectionError
    assert not server_enabled(cfg, 'ssh')


# -- fail2ban

def test_fail2ban(cfg, monkeypatch):
    import x84.fail2ban
    monkeypatch.setattr(x84.fail2ban, 'BANNED_IP_LIST', {})
    monkeypatch.setattr(x84.fail2ban, 'ATTEMPTED_LOGINS', {})
    cfg.add_section('fail2ban')
    cfg.set('fail2ban', 'enabled', 'yes')
    cfg.set('fail2ban', 'max_attempted_logins', '2')
    cfg.set('fail2ban', 'ip_blacklist', '10.0.0.1 10.0.0.2,10.0.0.3')
    cfg.set('fail2ban', 'ip_whitelist', '127.0.0.1')
    check = x84.fail2ban.get_fail2ban_function()
    assert not check('10.0.0.1')
    assert not check('10.0.0.3')
    results = [check('192.168.1.1') for _ in range(5)]
    assert results[:3] == [True, True, True]
    assert results[-1] is False
    assert all(check('127.0.0.1') for _ in range(10))


def test_fail2ban_disabled(cfg):
    import x84.fail2ban
    check = x84.fail2ban.get_fail2ban_function()
    assert all(check('192.168.1.1') for _ in range(100))
