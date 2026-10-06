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
