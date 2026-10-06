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
