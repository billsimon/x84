""" Tests of multi relay chat: the relay of the engine, and its script. """
# std imports
import logging
import platform
import queue
import socket
import threading
import time

# 3rd-party
import pyte
import pytest

# local
import x84.mrc
from x84.mrc import (Hub, Packet, Relay, ascii_text, decode_line,
                     format_packet, name_tag, parse_packet)
from tests.screens import Engine, PAUSE

SCRIPT = 'mrcchat'


def wait_for(predicate, timeout=5):
    """ Wait for ``predicate`` to be true; returns whether it was. """
    stime = time.time()
    while time.time() - stime < timeout:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeMRC(threading.Thread):

    """
    An MRC server of one bbs, answering as the server does: a packet of a
    user is sent back, as to every bbs; ``USERLIST`` is answered of
    ``users``, and ``ping`` and ``lines`` are sent of :meth:`push`.
    """

    def __init__(self, users=u'biff,marty'):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(2)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.users = users
        self.received = []
        self.connections = 0
        self.conn = None
        self.outbox = queue.Queue()
        self.done = threading.Event()
        self.lock = threading.Lock()

    def push(self, line):
        """ Send ``line`` to the bbs. """
        self.outbox.put(line)

    def drop(self):
        """ Disconnect the bbs. """
        self.outbox.put(None)

    def lines(self):
        with self.lock:
            return list(self.received)

    def bodies(self, user=None):
        """ Messages received, of every packet, or of ``user``. """
        return [line.split(u'~')[6] for line in self.lines()
                if line.count(u'~') >= 7 and
                user in (None, line.split(u'~')[0])]

    def run(self):
        while not self.done.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            self.connections += 1
            self.serve(conn)
        self.sock.close()

    def serve(self, conn):
        conn.settimeout(0.05)
        buf = b''
        while not self.done.is_set():
            try:
                line = self.outbox.get_nowait()
            except queue.Empty:
                pass
            else:
                if line is None:
                    conn.close()
                    return
                conn.sendall(line.encode('utf8') + b'\r\n')
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                conn.close()
                return
            buf += data
            while b'\n' in buf:
                raw, buf = buf.split(b'\n', 1)
                line = raw.decode('ascii')
                with self.lock:
                    self.received.append(line)
                self.answer(conn, line)
        conn.close()

    def answer(self, conn, line):
        packet = parse_packet(line)
        if packet is None or packet.from_user == u'CLIENT':
            return
        reply = None
        if packet.to_user == u'SERVER':
            if packet.body == u'USERLIST':
                reply = Packet(u'SERVER', u'', u'', packet.from_user, u'',
                               packet.from_room,
                               u'USERLIST:' + self.users)
        else:
            reply = packet
        if reply is not None:
            conn.sendall(format_packet(reply).encode('ascii') + b'\n')


@pytest.fixture
def mrc_server():
    server = FakeMRC()
    server.start()
    yield server
    server.done.set()
    server.join()


@pytest.fixture
def relay(mrc_server):
    relay = Relay(u'127.0.0.1', mrc_server.port, use_ssl=False,
                  bbsname=u'|10Bttf BBS',
                  info=((u'INFOWEB', u'https://bttf.example'),))
    relay.start()
    yield relay
    relay.stop()
    relay.join(5)


def received(relay, kind=None, timeout=5):
    """ Return payloads the relay received for sessions, within timeout. """
    result = []
    stime = time.time()
    while time.time() - stime < timeout:
        try:
            result.append(relay.received.get(timeout=0.05))
        except queue.Empty:
            if result:
                break
    return [item for item in result if kind is None or item[1][0] == kind]


# packets


def test_packets():
    line = u'biff~Bttf_BBS~lobby~~~lobby~|07hello~'
    packet = parse_packet(line)
    assert packet == Packet(u'biff', u'Bttf_BBS', u'lobby', u'', u'',
                            u'lobby', u'|07hello')
    assert format_packet(packet) == line
    assert parse_packet(u'not~a~packet') is None
    # a field is of printable ascii: ~ is a space, controls are removed.
    assert format_packet(packet._replace(body=u'a~b\r\nc\x1bd')) == (
        u'biff~Bttf_BBS~lobby~~~lobby~a bcd~')
    assert ascii_text(u'caf\u00e9 \u00ff\u20ac') == u'cafe y?'
    assert name_tag(u'|10Back to the |12Future') == u'Back_to_the_Future'
    assert decode_line(u'caf\u00e9'.encode('utf8')) == u'caf\u00e9'
    assert decode_line(b'caf\x82') == u'caf\u00e9'


# colors


def test_colors(script, session):
    # bright colors are those of the terminal, or bold, of an ansi
    # terminal of 8 colors; dark gray is not black.
    import blessed
    mrcchat = script(SCRIPT)
    Style = mrcchat.Style
    xterm = blessed.Terminal(kind='xterm-256color', force_styling=True)
    ansi = blessed.Terminal(kind='ansi', force_styling=True)
    assert mrcchat.style_sequence(xterm, Style(8, None)).endswith(
        u'\x1b[90m')
    assert mrcchat.style_sequence(xterm, Style(12, 1)).endswith(
        u'\x1b[91m\x1b[44m')
    assert mrcchat.style_sequence(xterm, Style(3, None)).endswith(
        u'\x1b[36m')
    assert mrcchat.style_sequence(ansi, Style(8, None)).endswith(
        u'\x1b[1m\x1b[30m')
    xterm8 = blessed.Terminal(kind='xterm', force_styling=True)
    assert xterm8.number_of_colors == 8
    assert mrcchat.style_sequence(xterm8, Style(8, None)).endswith(
        u'\x1b[1m\x1b[30m\x1b[90m')

    # black of a black background is hidden, written as spaces; black of
    # another background is not.
    screen = mrcchat.ChatScreen(session.term, 'utf8')
    screen.add(mrcchat.parse_pipes(u'|08-|00.|07Use |16|00__|20|00red'))
    assert screen.entries[-1][1] == [
        (Style(8, None), u'-'), (mrcchat.PLAIN, u' '), (Style(7, None), u'Use '),
        (mrcchat.PLAIN, u'  '), (Style(0, 4), u'red')]


# relay


def test_relay(mrc_server, relay):
    # the relay begins the connection as of the bbs, answers pings, and
    # passes packets: those of the server to every session, those of a
    # session to the server, but for those it answers itself.
    assert received(relay) == [(None, ('status', 'connected'))]
    system = u'{0}_{1}'.format(platform.system(), platform.machine())
    assert wait_for(lambda: len(mrc_server.lines()) >= 4)
    assert mrc_server.lines()[:4] == [
        u'|10Bttf BBS~X84/{0}/1.3.7'.format(system.replace(u' ', u'_')),
        u'CLIENT~Bttf_BBS~~SERVER~ALL~~INFOWEB:https://bttf.example~',
        u'CLIENT~Bttf_BBS~~SERVER~~~IMALIVE:Bttf_BBS~',
        u'CLIENT~Bttf_BBS~~SERVER~~~CAPABILITIES:MCI CTCP~',
    ]

    mrc_server.push(u'SERVER~~~~~~ping~')
    assert wait_for(lambda: mrc_server.lines().count(
        u'CLIENT~Bttf_BBS~~SERVER~~~IMALIVE:Bttf_BBS~') == 2)

    mrc_server.push(u'SERVER~~~~~~STATS:12 5 30 2~')
    line = u'biff~Bttf_BBS~lobby~~~lobby~hi there~'
    relay.send('sid-1', line)
    assert received(relay) == [
        (None, ('packet', u'SERVER~~~~~~STATS:12 5 30 2~')),
        (None, ('packet', line))]
    assert relay.stats == u'12 5 30 2'
    assert line in mrc_server.lines()

    # latency, statistics, and version, of a session, are of the relay.
    relay.send('sid-1', u'biff~Bttf_BBS~lobby~CLIENT~Bttf_BBS~lobby~LATENCY~')
    relay.send('sid-2', u'biff~Bttf_BBS~lobby~CLIENT~Bttf_BBS~lobby~STATS~')
    relay.send('sid-1', u'biff~Bttf_BBS~lobby~SERVER~Bttf_BBS~lobby~VERSION~')
    replies = received(relay)
    assert replies[0] == ('sid-1', ('packet', u'SERVER~~~CLIENT~Bttf_BBS~~'
                                    u'LATENCY:{0}~'.format(relay.latency)))
    assert replies[1] == ('sid-2', ('packet', u'SERVER~~~CLIENT~Bttf_BBS~~'
                                    u'STATS:12 5 30 2~'))
    assert replies[2] == ('sid-1', ('packet', u'CLIENT~~~biff~Bttf_BBS~~'
                                    u'|07- x/84 Multi Relay Chat v1.3.7~'))
    assert not any(u'LATENCY' in body or body == u'STATS'
                   for body in mrc_server.bodies())
    assert wait_for(lambda: u'VERSION' in mrc_server.bodies())

    # statistics are requested from time to time.
    assert wait_for(lambda: u'STATS' in mrc_server.bodies())


def test_relay_reconnects(mrc_server, relay, monkeypatch):
    # a connection lost is connected again, telling sessions of it; a
    # stopped relay tells the server.
    monkeypatch.setattr(x84.mrc, 'RETRY_INTERVALS', (0.1,))
    assert received(relay) == [(None, ('status', 'connected'))]
    mrc_server.drop()
    assert received(relay) == [(None, ('status', 'disconnected'))]
    assert received(relay, timeout=10) == [(None, ('status', 'connected'))]
    assert mrc_server.connections == 2
    relay.stop()
    relay.join(5)
    assert not relay.is_alive()
    assert wait_for(lambda: u'SHUTDOWN' in mrc_server.bodies())


def test_relay_too_old(mrc_server, relay):
    # a server refusing the version of the protocol ends the relay.
    assert received(relay) == [(None, ('status', 'connected'))]
    mrc_server.push(u'SERVER~~~~~~OLDVERSION:1.4.0~')
    relay.join(5)
    assert not relay.is_alive()
    assert received(relay)[-1] == (None, ('status', 'disabled'))
    assert mrc_server.connections == 1


def test_relay_unreachable(free_port, monkeypatch, caplog):
    # a server that can not be reached is tried again.
    monkeypatch.setattr(x84.mrc, 'RETRY_INTERVALS', (0.1,))
    relay = Relay(u'127.0.0.1', free_port(), use_ssl=False)
    with caplog.at_level(logging.INFO, logger='x84.mrc'):
        relay.start()
        assert wait_for(lambda: sum(u'connecting to' in record.message
                                    for record in caplog.records) >= 4)
        relay.stop()
        relay.join(5)
    assert relay.received.empty()


# hub


class Pipe(object):

    def __init__(self):
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)


class TTY(object):

    def __init__(self):
        self.master_write = Pipe()


class FakeRelay(object):

    state = 'connected'

    def __init__(self):
        self.received = queue.Queue()
        self.sent = []

    def send(self, sid, line):
        self.sent.append((sid, line))


def test_hub():
    # sessions in chat receive what the relay receives; one that ends
    # without leaving is logged off.
    relay = FakeRelay()
    hub = Hub(relay)
    terminals = [('a', TTY()), ('b', TTY()), ('c', TTY())]
    hub.session_event('a', ('join', u'biff'))
    hub.session_event('b', ('join', u'marty'))
    hub.session_event('c', ('send', u'doc~Bttf~lobby~~~lobby~not in chat~'))
    hub.session_event('a', ('send', u'biff~Bttf~lobby~~~lobby~hello~'))
    hub.session_event('b', ('send', u'not a packet'))
    relay.received.put((None, ('packet', u'one')))
    relay.received.put(('b', ('packet', u'two')))
    hub.deliver(terminals)
    assert terminals[0][1].master_write.sent == [
        ('mrc', ('status', 'connected')), ('mrc', ('packet', u'one'))]
    assert terminals[1][1].master_write.sent == [
        ('mrc', ('status', 'connected')), ('mrc', ('packet', u'one')),
        ('mrc', ('packet', u'two'))]
    assert terminals[2][1].master_write.sent == []
    assert relay.sent == [('a', u'biff~Bttf~lobby~~~lobby~hello~')]

    hub.session_event('b', ('leave',))
    del terminals[0]
    hub.deliver(terminals)
    assert hub.sessions == {}
    assert relay.sent[1:] == [
        (None, u'biff~Bttf~lobby~NOTME~~~|07- |12biff |04has left chat.~'),
        (None, u'biff~Bttf~lobby~SERVER~Bttf~lobby~LOGOFF~')]

    # without a relay, sessions are told chat is not available.
    hub = Hub()
    tty = TTY()
    hub.session_event('a', ('join', u'biff'))
    hub.session_event('a', ('send', u'biff~Bttf~lobby~~~lobby~hello~'))
    hub.deliver([('a', tty)])
    assert tty.master_write.sent == [('mrc', ('status', 'disabled'))]


# script


class MRCEngine(Engine):

    """ The engine of a test session, of a hub of multi relay chat. """

    def __init__(self, harness, hub):
        super().__init__(harness)
        self.hub = hub

    def run(self):
        import logging
        from x84.db import DBHandler, LOCKS
        from x84.engine import handle_lock
        pipe = self.pipe

        class TTY(object):
            sid = self.harness.session.sid
            master_write = pipe
        terminals = [(TTY.sid, TTY)]
        log = logging.getLogger('test')
        while not self.stopped:
            self.hub.deliver(terminals)
            if not self.harness.master_read.poll(0.02):
                continue
            event, data = self.harness.master_read.recv()
            if event.startswith('db'):
                DBHandler(self.pipe, event, data).start()
            elif event.startswith('lock'):
                handle_lock(LOCKS, TTY, event, data, False, log)
            elif event == 'output':
                self.output.append(data[0])
            elif event == 'mrc':
                self.hub.session_event(TTY.sid, data)


def start_engine(engine, session, hub):
    """ Return a :class:`MRCEngine` of ``hub``, in place of ``engine``. """
    engine.stopped = True
    engine.join()
    mrc_engine = MRCEngine(session, hub)
    mrc_engine.start()
    return mrc_engine


@pytest.fixture
def chat(session, users, engine, mrc_server, cfg):
    """ Return an engine of a relay connected to the fake server. """
    cfg.set('system', 'bbsname', 'Bttf BBS')
    relay = Relay(u'127.0.0.1', mrc_server.port, use_ssl=False,
                  bbsname=u'Bttf BBS')
    relay.start()
    engine = start_engine(engine, session, Hub(relay))
    assert wait_for(lambda: relay.state == 'connected')
    yield engine
    engine.stopped = True
    engine.join()
    relay.stop()
    relay.join(5)


def run_chat(engine, mrcchat, keys):
    """ Run the chat, sending ``keys`` (a number of them pauses). """
    def send():
        time.sleep(0.5)
        for key in keys:
            if callable(key):
                key()
            elif isinstance(key, float):
                time.sleep(key)
            else:
                engine.keys(key)
                time.sleep(0.1)

    sender = threading.Thread(target=send)
    sender.start()
    try:
        mrcchat.main()
    finally:
        sender.join()
    time.sleep(0.1)
    return engine.text


def screen_of(output, rows=24, columns=80):
    screen = pyte.Screen(columns, rows)
    pyte.Stream(screen).feed(output)
    return screen


def test_chat(chat, script, mrc_server):
    # entering chat announces the user, and joins the default room; what
    # is said is sent, and displayed as the server sends it back; a
    # mention is marked; a quit is told; the screen keeps its shape.
    mrcchat = script(SCRIPT)
    output = run_chat(chat, mrcchat, [
        1.0,
        lambda: mrc_server.push(
            u'SERVER~~~marty~~lobby~ROOMTOPIC:lobby:Great Scott!~'),
        lambda: mrc_server.push(
            u'biff~Hill_Valley~lobby~~~lobby~|03<|11biff|03>|16|07 '
            u'|07hey marty, nice vest~'),
        lambda: mrc_server.push(
            u'doc~Hill_Valley~lab~~~lab~|07another room~'),
        lambda: mrc_server.push(
            u'doc~Hill_Valley~lobby~JENNIFER~~~|07not for marty~'),
        lambda: mrc_server.push(
            u'doc~Hill_Valley~lobby~~~lobby~|07evil \x1b[2J\x1b]0;x\x07 '
            u'done~'),
        u'hello, biff\r', u'/me waves\r', 1.0,
        u'/quit see you\r'])
    bodies = mrc_server.bodies(u'marty')
    assert bodies[:7] == [
        u'|07- |11marty |03has arrived!', u'IAMHERE', u'TERMSIZE:80x24',
        u'BBSMETA: SecLevel(10) SysOp()', u'NEWROOM::lobby', u'USERLIST',
        u'USERLIST']
    assert (u'marty~Bttf_BBS~lobby~~~lobby~|03<|11marty|03>|16|07 '
            u'|07hello, biff~') in mrc_server.lines()
    assert u'|15* |13marty waves' in bodies
    assert bodies[-2:] == [
        u'|07- |12marty|04 has left chat |07(|15see you|07)', u'LOGOFF']
    assert not any(u'LATENCY' in body for body in bodies)

    # the screen, before leaving: before the message of it is written on
    # the last row.
    before = output[:output.rindex(u'\x1b[24;1H', 0,
                                   output.rindex(u'You have left chat'))]
    screen = screen_of(before)
    text = u'\n'.join(screen.display)
    assert screen.display[0].startswith(u'#lobby  Great Scott!')
    # biff and marty, of the server; doc, of what they said.
    assert u'3 users' in screen.display[0]
    assert u'<biff> hey marty, nice vest' in text
    assert u'<marty> hello, biff' in text
    assert u'* marty waves' in text
    assert u'another room' not in text and u'not for marty' not in text
    assert u'evil [2J]0;x done' in text
    assert screen.display[-1].startswith(u'[#lobby]')
    assert all(row[79] == u' ' for row in screen.display[1:-1])
    # the mention is marked, of the time of its message.
    row = next(idx for idx, line in enumerate(screen.display)
               if u'nice vest' in line)
    assert screen.buffer[row][0].bg == 'red'


def test_direct_messages(chat, script, mrc_server):
    # a direct message is displayed, and replied to by /r; a user is
    # ignored by /twit.
    mrcchat = script(SCRIPT)
    output = run_chat(chat, mrcchat, [
        1.0,
        lambda: mrc_server.push(
            u'biff~Hill_Valley~lobby~MARTY~~~|15* |08(|15biff|08/|14DirectMsg'
            u'|08) |07buttHead~'),
        0.5, u'/r who are you calling\r', u'/twit add biff\r', 0.5,
        lambda: mrc_server.push(
            u'biff~Hill_Valley~lobby~~~lobby~|07ignored words~'),
        0.5, u'/quit\r'])
    assert (u'marty~Bttf_BBS~lobby~BIFF~~~|15* |08(|15marty|08/|14DirectMsg'
            u'|08) |07who are you calling~') in mrc_server.lines()
    text = u'\n'.join(screen_of(output[:output.rindex(u'left chat')])
                      .display)
    assert u'(biff/DirectMsg) buttHead' in text
    assert u'(DirectMsg->BIFF) who are you calling' in text
    assert u'ignored words' not in text
    from x84.bbs.userbase import get_user
    assert get_user(u'marty')['mrc_twits'] == [u'biff']


def test_settings(chat, script, mrc_server):
    # a user's settings change what is said, and are kept.
    mrcchat = script(SCRIPT)
    run_chat(chat, mrcchat, [
        1.0, u'/set nickcolor |14\r', u'/set rtbracket |04]\r',
        u'/set nickcolor red\r', u'/join #DeLorean\r', u'1.21 gigawatts\r',
        0.5, u'/quit\r'])
    bodies = mrc_server.bodies()
    assert u'NEWROOM:lobby:delorean' in bodies
    assert (u'marty~Bttf_BBS~delorean~~~delorean~|03<|14marty|04]|16|07 '
            u'|071.21 gigawatts~') in mrc_server.lines()
    assert (u'marty~Bttf_BBS~lobby~NOTME~~lobby~|07- |02marty |10has left '
            u'the room.~') in mrc_server.lines()
    from x84.bbs.userbase import get_user
    assert get_user(u'marty')['mrc'] == {u'nickcolor': u'|14',
                                         u'rtbracket': u'|04]'}


def test_reconnect(chat, script, mrc_server, monkeypatch):
    # a connection lost and connected again is told, and the user is
    # announced again, in their room.
    monkeypatch.setattr(x84.mrc, 'RETRY_INTERVALS', (0.1,))
    mrcchat = script(SCRIPT)
    output = run_chat(chat, mrcchat, [
        1.0, u'/join hill_valley\r', 0.5, mrc_server.drop, 2.0,
        u'/quit\r'])
    assert mrc_server.connections == 2
    assert mrc_server.bodies().count(u'NEWROOM::hill_valley') == 1
    text = u'\n'.join(screen_of(output[:output.rindex(u'left chat')]).display)
    assert u'connection to MRC is lost' in text
    assert u'Connected to MRC again' in text


def test_not_available(session, users, engine, script):
    # without a relay, the chat says so, and returns.
    engine = start_engine(engine, session, Hub())
    try:
        mrcchat = script(SCRIPT)
        output = run_chat(engine, mrcchat, [PAUSE, u'\r'])
    finally:
        engine.stopped = True
        engine.join()
    assert u'not available' in output


def test_cp437_terminal(chat, script, mrc_server, session):
    # a terminal of cp437 displays what is not of it, folded, and the
    # screen keeps its shape.
    session.session.encoding = 'cp437'
    mrcchat = script(SCRIPT)
    output = run_chat(chat, mrcchat, [
        1.0,
        lambda: mrc_server.push(
            u'doc~Hill_Valley~lobby~~~lobby~|07caf\u00e9 \u0142\u00f3d\u017a '
            u'\u2603 ' + u'x' * 90 + u'~'),
        0.5, u'/quit\r'])
    output.encode('cp437')
    screen = screen_of(output[:output.rindex(u'left chat')])
    assert u'caf\u00e9 ?\u00f3dz ?' in u'\n'.join(screen.display)
    assert all(row[79] == u' ' for row in screen.display[1:-1])
