""" Tests for the telnet server protocol handling of x/84. """
# std imports
import struct

# local
from x84.bbs.telnet import (
    IAC, SB, SE, DO, WILL, WONT, TTYPE, NAWS, NEW_ENVIRON, IS, SEND, ECHO,
    BINARY, SGA, TelnetClient as TelnetClientConnection)
from x84.telnet import TelnetClient


class FakeSocket(object):
    """ Socket stand-in that records sent data and returns queued data. """

    def __init__(self, received=b''):
        self.received = received
        self.sent = bytearray()

    def recv(self, size):
        data, self.received = self.received[:size], self.received[size:]
        return data

    def send(self, data):
        self.sent.extend(data)
        return len(data)

    def sendall(self, data):
        self.sent.extend(data)

    def fileno(self):
        return -1


def make_client(data=b''):
    client = TelnetClient(FakeSocket(data), ('127.0.0.1', 1234))
    return client


def feed(client, data):
    client.sock.received = data
    while client.sock.received:
        client.socket_recv()


def test_plain_input_is_buffered():
    client = make_client()
    feed(client, b'hello\r\n')
    assert client.get_input() == b'hello\r\n'


def test_escaped_iac_is_literal_0xff():
    client = make_client()
    feed(client, b'a' + IAC + IAC + b'b')
    assert client.get_input() == b'a\xffb'


def test_ttype_subnegotiation():
    client = make_client()
    feed(client, IAC + SB + TTYPE + IS + b'XTERM-256COLOR' + IAC + SE)
    assert client.env['TERM'] == 'xterm-256color'
    assert client.get_input() == b''


def test_naws_subnegotiation():
    received = []
    client = TelnetClient(FakeSocket(), ('127.0.0.1', 1),
                          on_naws=received.append)
    # 132 columns, 300 rows: 300 is 0x012c.
    feed(client, IAC + SB + NAWS + struct.pack('!HH', 132, 300) + IAC + SE)
    assert (client.env['COLUMNS'], client.env['LINES']) == ('132', '300')
    assert received == [client]


def test_naws_with_escaped_iac():
    client = make_client()
    # a width of 255 columns must be escaped as IAC IAC.
    feed(client, IAC + SB + NAWS + b'\x00' + IAC + IAC + b'\x00\x18'
         + IAC + SE)
    assert (client.env['COLUMNS'], client.env['LINES']) == ('255', '24')


def test_new_environ_all_variables():
    # the final variable is not followed by a delimiter; it was previously
    # discarded.
    client = make_client()
    payload = (b'\x00USER\x01jojo\x00LANG\x01en_US.UTF-8'
               b'\x03CUSTOM\x01value')
    feed(client, IAC + SB + NEW_ENVIRON + IS + payload + IAC + SE)
    assert client.env['USER'] == 'jojo'
    assert client.env['LANG'] == 'en_US.UTF-8'
    assert client.env['CUSTOM'] == 'value'


def test_send_unicode_escapes_iac():
    client = make_client()
    # latin-1 encodes U+00FF as byte 0xff, which must be escaped.
    client.send_unicode(u'\xff', encoding='latin-1')
    assert bytes(client.send_buffer) == IAC + IAC


def test_request_env_is_bytes():
    client = make_client()
    client.request_env()
    sent = bytes(client.send_buffer)
    assert sent.startswith(IAC + SB + NEW_ENVIRON + SEND)
    assert sent.endswith(IAC + SE)


def test_will_echo_is_refused():
    import pytest
    from x84.bbs.exception import Disconnected
    client = make_client()
    with pytest.raises(Disconnected):
        feed(client, IAC + WILL + ECHO)


def test_will_binary_is_agreed():
    client = make_client()
    feed(client, IAC + WILL + BINARY)
    assert bytes(client.send_buffer) == IAC + DO + BINARY
    assert client.check_remote_option(BINARY) is True


def test_telnet_client_connection_strips_commands():
    # the outbound telnet client used by the default board's telnet.py
    conn = TelnetClientConnection(term_type='ansi', width=80, height=25)
    conn.sock = FakeSocket()
    data = conn._process(b'hi' + IAC + WILL + ECHO + IAC + DO + TTYPE +
                         IAC + SB + TTYPE + SEND + IAC + SE + b'there' +
                         IAC + IAC)
    assert data == b'hithere\xff'
    sent = bytes(conn.sock.sent)
    assert IAC + DO + ECHO in sent
    assert IAC + WILL + TTYPE in sent
    assert IAC + SB + TTYPE + IS + b'ansi' + IAC + SE in sent


def test_telnet_client_connection_naws():
    conn = TelnetClientConnection(term_type='ansi', width=80, height=25)
    conn.sock = FakeSocket()
    conn._process(IAC + DO + NAWS)
    assert (IAC + SB + NAWS + struct.pack('!HH', 80, 25) + IAC + SE
            in bytes(conn.sock.sent))


def test_telnet_client_connection_refuses_unknown():
    conn = TelnetClientConnection()
    conn.sock = FakeSocket()
    conn._process(IAC + DO + SGA + IAC + DO + b'\x63')
    assert bytes(conn.sock.sent) == IAC + WILL + SGA + IAC + WONT + b'\x63'
