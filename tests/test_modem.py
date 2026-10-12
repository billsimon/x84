""" Tests for dial-in modem callers: x84.modem and x84.modem_bridge. """
# std imports
import json
import logging
import socket

# 3rd-party
import pytest

# local
from x84.modem import parse_header, clean_value, ModemClient
from x84.modem_bridge import make_header


def test_parse_header():
    call = parse_header(json.dumps({
        'rate': 14400, 'connect': '14400 V.42/V.42bis', 'caller_id': '+15551234',
        'caller_name': 'Alice', 'modulation': 'v32bis', 'protocol': 'V.42/V.42bis',
        'called_id': '5550000', 'unknown': 'ignored'}).encode())
    assert call == {
        'rate': '14400', 'connect': '14400 V.42/V.42bis', 'caller_id': '+15551234',
        'caller_name': 'Alice', 'modulation': 'v32bis', 'protocol': 'V.42/V.42bis',
        'called_id': '5550000'}


def test_parse_header_cleans_values():
    call = parse_header(json.dumps({
        'caller_name': u'Evil\x1b[2J\r\nName', 'caller_id': '9' * 500,
        'rate': 'fast'}).encode())
    assert call['caller_name'] == u'Evil[2JName'
    assert len(call['caller_id']) == 128
    assert call['rate'] == '0'
    assert call['connect'] == ''
    assert clean_value(None) == ''


@pytest.mark.parametrize('line', [b'not json', b'[1, 2]', b'\xff\xfe', b''])
def test_parse_header_malformed(line):
    with pytest.raises(ValueError):
        parse_header(line)


def test_parse_header_secret():
    line = json.dumps({'rate': 2400, 'secret': 's3cret'}).encode()
    assert parse_header(line, secret='s3cret')['rate'] == '2400'
    with pytest.raises(ValueError):
        parse_header(line, secret='other')
    with pytest.raises(ValueError):
        parse_header(json.dumps({'rate': 2400}).encode(), secret='s3cret')


def test_bridge_header_from_environment():
    environ = {'CONNECT': '9600 V.42', 'CALLER_ID': '5551234',
               'CALLER_NAME': 'Bob', 'MODEM_MODULATION': 'v32'}
    line = make_header(environ, secret='key')
    assert line.endswith(b'\n') and line.count(b'\n') == 1
    header = json.loads(line)
    # rate is taken from CONNECT, when MODEM_RATE is not given.
    assert header['rate'] == '9600'
    assert header['caller_id'] == '5551234'
    assert header['secret'] == 'key'
    assert parse_header(line.rstrip(b'\n'), secret='key')['rate'] == '9600'


def test_modem_client_session_id():
    sock_a, sock_b = socket.socketpair()
    try:
        client = ModemClient(sock_a, ('127.0.0.1', 4321))
        assert client.addrport == 'unknown:4321'
        client.caller_id = u'+1 (555) 123-4567'
        assert client.addrport == '+1555123-4567:4321'
    finally:
        sock_a.close()
        sock_b.close()


# -- rate-aware artwork

@pytest.fixture
def modem_session(session):
    session.session.kind = 'modem'
    return session


@pytest.mark.parametrize('rate,min_rate,expected', [
    ('300', None, 300),
    ('1200', '2400', 1200),
    ('2400', '2400', 0),
    ('14400', None, 0),
    ('300', '0', 0),
    ('', None, 0),
])
def test_slow_modem_rate(modem_session, cfg, rate, min_rate, expected):
    from x84.bbs.output import slow_modem_rate
    modem_session.session.env['MODEM_RATE'] = rate
    if min_rate is not None:
        cfg.set('modem', 'art_min_rate', min_rate)
    assert slow_modem_rate() == expected


def test_slow_modem_rate_only_for_modems(session):
    from x84.bbs.output import slow_modem_rate
    session.session.env['MODEM_RATE'] = '300'
    assert slow_modem_rate() == 0


def test_showart_skipped_at_slow_rate(modem_session, tmp_path):
    from x84.bbs import showart
    artfile = tmp_path / 'big.ans'
    artfile.write_bytes(b'\xdb' * 70 + b'\r\n')
    modem_session.session.env['MODEM_RATE'] = '1200'
    text = u''.join(showart(str(artfile)))
    assert u'big.ans not shown at 1200 bps' in text
    assert u'█' not in text
    # force shows it anyway, and faster callers see it.
    assert u'█' in u''.join(showart(str(artfile), force=True))
    modem_session.session.env['MODEM_RATE'] = '9600'
    assert u'█' in u''.join(showart(str(artfile)))


# -- engine

class FakeClient(object):
    SEND_HIGH_WATER = 10

    def __init__(self, buffered):
        self.send_buffer = bytearray(buffered)


def test_output_blocked():
    from x84.engine import output_blocked
    assert not output_blocked(FakeClient(9))
    assert output_blocked(FakeClient(10))
    assert ModemClient.SEND_HIGH_WATER < 4096


def test_session_output_not_read_while_client_backlogged(monkeypatch):
    """ The engine stops reading a session's pipe while its client lags. """
    import multiprocessing
    import x84.engine
    from x84.terminal import TerminalProcess

    master_read, child_write = multiprocessing.Pipe(duplex=False)
    child_read, master_write = multiprocessing.Pipe(duplex=False)
    client = FakeClient(10)
    sent = []
    client.send_unicode = lambda ucs, encoding: sent.append(ucs)

    class Server(object):
        clients = {1: client}

    tty = TerminalProcess.__new__(TerminalProcess)
    tty.client, tty.sid = client, 'test-sid'
    tty.master_read, tty.master_write = master_read, master_write
    tty.timeout, tty.process = 0, None
    monkeypatch.setattr(x84.engine, 'find_tty', lambda _client: tty)

    child_write.send(('output', (u'hello', 'utf8')))
    log = logging.getLogger('test')
    assert x84.engine.get_session_output_fds([Server()]) == []
    x84.engine.session_recv([('test-sid', tty)], log, False)
    assert sent == []

    # once the client catches up, its output is read again.
    client.send_buffer = bytearray()
    assert x84.engine.get_session_output_fds([Server()]) == [
        master_read.fileno()]
    x84.engine.session_recv([('test-sid', tty)], log, False)
    assert sent == [u'hello']
    for conn in (master_read, child_write, child_read, master_write):
        conn.close()


def test_modem_calls_exempt_from_fail2ban(monkeypatch):
    """ All modem calls arrive from the bridge on 127.0.0.1. """
    import x84.engine
    from x84.modem import ModemServer
    from x84.telnet import TelnetServer

    for server_class, expect_checked in ((TelnetServer, True),
                                         (ModemServer, False)):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        peer = socket.create_connection(listener.getsockname())
        checked = []
        server = server_class.__new__(server_class)
        server.server_socket = listener
        server.clients, server.threads = {}, []

        def deny(ip):
            checked.append(ip)
            return False
        x84.engine.accept(logging.getLogger('test'), server, deny)
        assert bool(checked) is expect_checked
        assert bool(server.clients) is not expect_checked
        for client in server.clients.values():
            client.sock.close()
        for thread in server.threads:
            thread.stopped = True
        peer.close()
        listener.close()
