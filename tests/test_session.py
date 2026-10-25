""" Tests for the session event engine of x/84. """
# std imports
import os
import threading
import time

# 3rd-party
import pytest

# local
from x84.bbs.exception import Disconnected


def test_input_order_preserved(session):
    """ Keyboard input buffered while awaiting other events keeps its order. """
    for key in 'abc':
        session.keys(key)
    session.send('refresh', ('resize', (100, 30)))
    sess = session.session
    # reading 'refresh' buffers all input received before it.
    assert sess.read_event('refresh', timeout=2) == ('resize', (100, 30))
    received = [sess.read_event('input', -1) for _ in range(3)]
    assert received == [b'a', b'b', b'c']


def test_pushback_is_received_first(session):
    sess = session.session
    session.keys('x')
    session.keys('y')
    first = sess.read_event('input', timeout=2)
    assert first == b'x'
    sess.buffer_input(first, pushback=True)
    assert sess.read_event('input', -1) == b'x'
    assert sess.read_event('input', timeout=2) == b'y'


def test_nonblocking_poll_reads_pipe(session):
    """ A non-blocking poll receives events already sent by the engine. """
    session.send('refresh', ('resize', (90, 25)))
    time.sleep(0.1)
    assert session.session.poll_event('refresh') == ('resize', (90, 25))
    assert session.session.poll_event('refresh') is None


def test_resize_sets_terminal_dimensions(session):
    session.send('refresh', ('resize', (132, 43)))
    session.session.read_event('refresh', timeout=2)
    assert (session.term.width, session.term.height) == (132, 43)


def test_global_broadcast_is_named_event(session):
    """ ('global', ('newmsg', 5)) is received as event 'newmsg'. """
    session.send('global', ('newmsg', 5))
    session.send('global', ('oneliner', True))
    assert session.session.read_event('newmsg', timeout=2) == 5
    assert session.session.read_event('oneliner', timeout=2) is True


def test_info_names_kind(session):
    # who's online shows the kind of each session's connection.
    session.session._node = 1
    assert session.session.to_dict()['kind'] == 'test'


def test_global_ayt_is_acknowledged(session):
    session.send('global', ('AYT', 'other-sid'))
    session.session.poll_event('anything')
    time.sleep(0.1)
    event, data = session.master_read.recv()
    while event == 'logger':
        event, data = session.master_read.recv()
    assert (event, data) == ('route', ('other-sid', 'ACK',
                                       session.session.sid, 'anonymous'))


def test_exception_event_is_raised(session):
    session.send('exception', Disconnected('goodbye'))
    with pytest.raises(Disconnected):
        session.session.read_event('input', timeout=2)


def test_closed_pipe_disconnects(session):
    session.master_write.close()
    with pytest.raises(Disconnected):
        session.session.read_event('input', timeout=2)


def test_inkey_decodes_utf8_across_events(session):
    """ A multi-byte character divided across two events is decoded. """
    snowman = u'☃'.encode('utf8')
    session.send('input', snowman[:1])
    session.send('input', snowman[1:])
    assert session.term.inkey(timeout=2) == u'☃'


def test_inkey_application_key(session):
    session.send('input', b'\x1b[A')
    key = session.term.inkey(timeout=2)
    assert key.code == session.term.KEY_UP


def test_inkey_timeout(session):
    stime = time.time()
    assert session.term.inkey(timeout=0.2) == u''
    assert time.time() - stime >= 0.15


def test_output_is_encoded_by_session(session):
    from x84.bbs import echo
    echo(u'hello')
    assert session.output() == u'hello'


class MiniEngine(threading.Thread):

    """ Services a session's database and lock events, as the engine does. """

    def __init__(self, harness):
        super().__init__(daemon=True)
        self.harness = harness
        self.stopped = False

    def run(self):
        import logging
        from x84.db import DBHandler, LOCKS
        from x84.engine import handle_lock

        class TTY(object):
            sid = self.harness.session.sid
            master_write = self.harness.master_write
        log = logging.getLogger('test')
        while not self.stopped:
            if not self.harness.master_read.poll(0.05):
                continue
            event, data = self.harness.master_read.recv()
            if event.startswith('db'):
                DBHandler(self.harness.master_write, event, data).start()
            elif event.startswith('lock'):
                handle_lock(LOCKS, TTY, event, data, False, log)


def test_database_over_session_ipc(session):
    """ User records saved by a session are stored by the engine. """
    from x84.bbs.userbase import User, get_user, find_user
    engine = MiniEngine(session)
    engine.start()
    try:
        user = User(u'remote')
        user.password = u'password'
        user.save()
        user['color'] = 'green'
        assert find_user(u'REMOTE') == u'remote'
        assert get_user(u'remote').get('color') == 'green'
        assert get_user(u'remote').auth(u'password')
        # node number is allocated by bbs-wide lock.
        assert session.session.node == 1
    finally:
        engine.stopped = True
        engine.join()


def test_server_environment_does_not_affect_sessions(monkeypatch):
    """ NO_COLOR, etc. of the server's own terminal must not apply. """
    from x84.terminal import init_term
    import x84.bbs.ini
    monkeypatch.setattr(x84.bbs.ini, 'CFG', x84.bbs.ini.init_bbs_ini())
    # (each is set by monkeypatch, so that it is restored afterwards.)
    for name, value in (('NO_COLOR', '1'), ('TERM', 'dumb'),
                        ('TERM_PROGRAM', 'Apple_Terminal'),
                        ('COLORTERM', '')):
        monkeypatch.setenv(name, value)

    class Writer(object):
        def send(self, data):
            pass
    term = init_term(Writer(), {'TERM': 'no-such-terminal', 'LINES': '24',
                                'COLUMNS': '80', 'COLORTERM': 'truecolor'})
    assert term.kind == 'ansi'
    assert term.does_styling
    assert term.red(u'x') != u'x'
    assert os.environ.get('COLORTERM') == 'truecolor'


def test_terminal_of_unknown_type_of_known_family(monkeypatch):
    """ A terminal type not of the database is of its family, else ansi. """
    from x84.terminal import family_kind, init_term
    import x84.bbs.ini
    monkeypatch.setattr(x84.bbs.ini, 'CFG', x84.bbs.ini.init_bbs_ini())
    for name in ('TERM', 'COLORTERM'):
        monkeypatch.setenv(name, '')

    class Writer(object):
        def send(self, data):
            pass
    term = init_term(Writer(), {'TERM': 'xterm-color', 'LINES': '24',
                                'COLUMNS': '80'})
    assert term.kind == 'xterm'
    assert family_kind('xterm-color', 'ansi') == 'xterm'
    assert family_kind('screen-color', 'ansi') == 'screen'
    assert family_kind('xterm', 'ansi') == 'ansi'
    assert family_kind('no-such-terminal', 'ansi') == 'ansi'


def test_chat_script_holds_idle_timeout(session, tmp_path):
    """ A script of IDLE_TIMEOUT = False holds the engine's idle timeout. """
    import types
    from x84.bbs.script_def import Script
    (tmp_path / 'chatty.py').write_text(
        'import x84.bbs.session\n'
        'from x84.bbs.script_def import Script\n'
        'IDLE_TIMEOUT = False\n'
        'def main(depth=0, fail=False):\n'
        '    run = x84.bbs.session.getsession().runscript\n'
        '    if depth:\n'
        '        run(Script("chatty", (depth - 1,), {}))\n'
        '        run(Script("plain", (), {}))\n'
        '    if fail:\n'
        '        raise ValueError("chat failed")\n'
        '    return depth\n')
    (tmp_path / 'plain.py').write_text('def main():\n    return None\n')
    sess = session.session
    sess._script_module = [types.SimpleNamespace(__path__=[str(tmp_path)])]

    def events():
        result = []
        while session.master_read.poll():
            event, data = session.master_read.recv()
            if event == 'hold-timeout':
                result.append(data)
        return result

    # held by the outermost of nested chat scripts, only.
    assert sess.runscript(Script('chatty', (2,), {})) == 2
    assert events() == [True, False]
    assert sess.runscript(Script('plain', (), {})) is None
    assert events() == []
    # and released when the script fails.
    with pytest.raises(ValueError):
        sess.runscript(Script('chatty', (), {'fail': True}))
    assert events() == [True, False]


def test_engine_holds_idle_timeout(cfg, monkeypatch):
    """ No session times out while held; idle time counts from release. """
    import logging
    import multiprocessing
    import types
    import x84.engine
    import x84.terminal
    from x84.terminal import TerminalProcess

    class Client(object):
        events, send_buffer, SEND_HIGH_WATER = [], b'', 4096
        last_input = 0

        def input_ready(self):
            return False

        def idle(self):
            return now[0] - self.last_input

    now = [1000.0]
    monkeypatch.setattr(x84.terminal, 'time',
                        types.SimpleNamespace(time=lambda: now[0]))
    killed = []
    monkeypatch.setattr(x84.engine, 'kill_session',
                        lambda client, reason: killed.append(reason))
    master_read, child_write = multiprocessing.Pipe(duplex=False)
    child_read, master_write = multiprocessing.Pipe(duplex=False)
    cfg.set('system', 'timeout', '60')
    tty = TerminalProcess(Client(), 'test-sid', (master_write, master_read))
    terminals = [('test-sid', tty)]

    now[0] = 59.0
    x84.engine.session_send(terminals)
    assert killed == []
    now[0] = 61.0
    x84.engine.session_send(terminals)
    assert killed == ['timeout']

    # the chat script holds the timeout, by the event of its session.
    del killed[:]
    child_write.send(('hold-timeout', True))
    x84.engine.session_recv(terminals, logging.getLogger('test'), False)
    assert tty.timeout_held
    now[0] = 7200.0
    x84.engine.session_send(terminals)
    assert killed == []

    # once released, the user idle in chat is given the timeout anew.
    child_write.send(('hold-timeout', False))
    x84.engine.session_recv(terminals, logging.getLogger('test'), False)
    assert not tty.timeout_held
    now[0] = 7259.0
    x84.engine.session_send(terminals)
    assert killed == []
    now[0] = 7261.0
    x84.engine.session_send(terminals)
    assert killed == ['timeout']
    for conn in (master_read, child_write, child_read, master_write):
        conn.close()


def test_chats_hold_idle_timeout(script):
    """ Users of irc and mrc chat are not disconnected for idling. """
    assert script('ircchat').IDLE_TIMEOUT is False
    assert script('mrcchat').IDLE_TIMEOUT is False
