""" Tests of the tn3270 server, x84/tn3270.py. """
# std imports
import socket
import threading
import time

# 3rd-party
import pytest

pytest.importorskip('pyte')

# local
from x84 import tn3270
from x84.tn3270 import (
    AID_ENTER, AID_PA1, AID_PA2, AID_PF, BLANK, CMD_EW, CMD_EWA, CMD_W,
    COLORS, EOR, FA_NONDISPLAY, HL_REVERSE, HL_UNDERSCORE, IAC,
    ORDER_SBA, QUERY, TTYPE, ConnectTN3270, TN3270Client, decode_address,
    encode_address, encode_attr, from_ebcdic, keys_of, parse_input,
    parse_query_reply, parse_ttype, style_cell, to_ebcdic,
)

DO, DONT, WILL, WONT, SB, SE = 253, 254, 251, 252, 250, 240
EOR_MARK = 239

#: features of a terminal of 7 colors, of 16 colors and background colors,
#: as the x3270 family, and of a basic one.
COLOR = ('bracket', 7, True, False)
COLOR16 = ('bracket', 16, True, True)
BASIC = ('bracket', 0, False, False)

#: a terminal's answer to a query: a 27x132 screen, of 16 colors and
#: extended highlighting, of code page 37, as by s3270.
QUERY_REPLY = (
    b'\x88'
    # usable area
    b'\x00\x0a\x81\x81\x01\x00\x00\x84\x00\x1b'
    # character sets: the base set, of CGCSID 697/37, and APL's, 963/310
    b'\x00\x1b\x81\x85\x82\x00\x09\x0c\x00\x00\x00\x00\x07'
    b'\x00\x10\x00\x02\xb9\x00\x25\x01\x00\xf1\x03\xc3\x01\x36'
    # color: the default, and 15 colors, each displayed
    b'\x00\x26\x81\x86\x00\x10\x00\xf4' + bytes(
        val for color in range(0xF1, 0x100) for val in (color, color)) +
    # highlighting
    b'\x00\x05\x81\x87\x00'
    # RPQ names: of device type and model 0, x3270
    b'\x00\x12\x81\xa1\x00\x00\x00\x00\x00\x00\x00\x00'
    b'\x06\xa7\xf3\xf2\xf7\xf0'
    # implicit partition
    b'\x00\x11\x81\xa6\x00\x00\x0b\x01\x00'
    b'\x00\x50\x00\x18\x00\x84\x00\x1b'
)

#: the answer of DX3270 to a query: of 7 colors, and no code page.
DX3270_REPLY = bytes(
    [0x88, 0x00, 0x08, 0x81, 0x80, 0x81, 0x84, 0x86, 0x87,
     0x00, 0x15, 0x86, 0x00, 0x08, 0x00, 0xF4] +
    [val for color in range(0xF1, 0xF8) for val in (color, color)] +
    [0x00, 0x0E, 0x87, 0x05, 0x00, 0x00, 0xF1, 0xF1, 0xF2, 0xF2,
     0xF4, 0xF4, 0xF8, 0xF8])


def test_address_round_trip():
    for pos in list(range(4096)) + [4096, 5000, 16383]:
        assert decode_address(*encode_address(pos)) == pos
    # 12-bit addresses are of the 6-bit code table.
    assert encode_address(0) == b'\x40\x40'
    assert encode_address(80) == b'\xc1\x50'


def test_parse_ttype():
    assert parse_ttype('IBM-3279-2-E') == (24, 80, True)
    assert parse_ttype('IBM-3278-4') == (43, 80, False)
    assert parse_ttype('ibm-3279-5-e') == (27, 132, True)
    assert parse_ttype('IBM-DYNAMIC') == (24, 80, True)
    assert parse_ttype('IBM-3277-2') == (24, 80, False)


def test_parse_query_reply():
    reply = parse_query_reply(QUERY_REPLY)
    assert reply['size'] == (27, 132)
    assert {0x81, 0x86, 0x87, 0xA6} <= reply['codes']
    assert reply['colors'] == 16
    assert reply['codepage'] == 37 and reply['name'] == u'x3270'
    # without an implicit partition, the usable area is the size.
    assert parse_query_reply(QUERY_REPLY[:11])['size'] == (27, 132)
    assert parse_query_reply(b'\x88') == {
        'codes': set(), 'size': None, 'colors': 0, 'codepage': None,
        'name': None}
    # a monochrome terminal names colors, none of them displayed; a 3279
    # displays 7.
    pairs = [(0x00, 0xF4)] + [(color, 0) for color in range(0xF1, 0x100)]
    mono = bytes((0x88, 0x00, 0x26, 0x81, 0x86, 0x00, 0x10)) + bytes(
        val for pair in pairs for val in pair)
    assert parse_query_reply(mono)['colors'] == 0
    pairs = [(0x00, 0xF4)] + [(color, color) for color in range(0xF1, 0xF8)]
    color = bytes((0x88, 0x00, 0x16, 0x81, 0x86, 0x00, 0x08)) + bytes(
        val for pair in pairs for val in pair)
    assert parse_query_reply(color)['colors'] == 7


def test_ebcdic():
    assert to_ebcdic(u'A', 'cp037') == 0xC1
    assert to_ebcdic(u' ', 'cp037') == 0x40
    # brackets, by code page.
    assert to_ebcdic(u'[', 'cp037') == 0xBA
    assert to_ebcdic(u'[', 'bracket') == 0xAD
    assert to_ebcdic(u']', 'bracket') == 0xBD
    assert from_ebcdic(b'\xad\xc1\xbd', 'bracket') == u'[A]'
    assert from_ebcdic(b'\xba\xc1\xbb', 'cp037') == u'[A]'
    # characters without an EBCDIC equal.
    assert to_ebcdic(u'─', 'cp037') == to_ebcdic(u'-', 'cp037')
    assert to_ebcdic(u'║', 'cp037') == to_ebcdic(u'|', 'cp037')
    assert to_ebcdic(u'┌', 'cp037') == to_ebcdic(u'+', 'cp037')
    assert to_ebcdic(u'░', 'cp037') == to_ebcdic(u'.', 'cp037')
    assert to_ebcdic(u'中', 'cp037') == to_ebcdic(u'?', 'cp037')
    assert to_ebcdic(u'\x07', 'cp037') == to_ebcdic(u'?', 'cp037')
    assert to_ebcdic(u'', 'cp037') == 0x40
    # accented latin characters are of the code page.
    assert from_ebcdic(bytes((to_ebcdic(u'\xe9', 'cp037'),)),
                       'cp037') == u'\xe9'


def test_keys_of():
    # the PF keys, as ISPF names them: PF3 END, PF7 UP, PF8 DOWN, and
    # PF12 CANCEL.  PF1, HELP, sends none, as PA1.
    assert keys_of(AID_ENTER, u'n') == u'n\r'
    assert keys_of(AID_ENTER, u'') == u'\r'
    assert keys_of(AID_PF[0], u'y') is None
    assert keys_of(AID_PF[3], u'y') == u'y'
    assert keys_of(AID_PF[2], u'') == u'\x1b'
    assert keys_of(AID_PF[11], u'') == u'\x1b'
    assert keys_of(AID_PF[7], u'') == u'\x1b[6~'
    # PF13-PF24 are the same as PF1-PF12, but PF16 and PF21.
    assert keys_of(AID_PF[14], u'') == u'\x1b'
    assert keys_of(AID_PF[12], u'') is None
    assert keys_of(AID_PF[15], u'') == u'\t'
    assert keys_of(AID_PF[20], u'') == u'\x1b[F'
    assert keys_of(AID_PA1, u'') is None
    assert tn3270.is_help(AID_PF[0]) and tn3270.is_help(AID_PA1)
    assert not tn3270.is_help(AID_PF[2])


def test_parse_input():
    record = (bytes((AID_ENTER,)) + encode_address(1845) + bytes(
        (ORDER_SBA,)) + encode_address(1845) + u'hi'.encode('cp037'))
    assert parse_input(record) == (AID_ENTER, 1845, {1845: b'\x88\x89'})
    assert parse_input(bytes((AID_PA2,))) == (AID_PA2, None, {})


def test_style_cell():
    def style(data, fg='default', bg='default', bold=False, reverse=False,
              features=COLOR):
        return style_cell(data, fg, bg, bold, reverse, False, False, False,
                          features)
    letter = to_ebcdic(u'x', 'bracket')
    assert style(u' ') == BLANK
    assert style(u'x') == (letter, COLORS['white'], 0, 0)
    assert style(u'x', 'brightred') == (letter, COLORS['red'], 0, 0)
    assert style(u'x', 'magenta') == (letter, COLORS['magenta'], 0, 0)
    assert style(u'x', 'ff8700') == (letter, COLORS['yellow'], 0, 0)
    # dark gray, of bold black, is blue.
    assert style(u'x', 'black', bold=True) == (letter, COLORS['blue'], 0, 0)
    # black on black is not displayed.
    assert style(u'x', 'black') == BLANK
    # a bar, of a background color, is in reverse video.
    assert style(u'x', 'yellow', 'blue') == (
        letter, COLORS['blue'], HL_REVERSE, 0)
    assert style(u'x', reverse=True) == (
        letter, COLORS['white'], HL_REVERSE, 0)
    assert style(u' ', 'white', 'blue') == (
        0x40, COLORS['blue'], HL_REVERSE, 0)
    # or of background color, when displayed.
    assert style(u'x', 'yellow', 'blue',
                 features=('bracket', 7, True, True)) == (
        letter, COLORS['yellow'], 0, COLORS['blue'])
    # of 16 colors, normal colors are of the extended colors, and bright
    # ones of those of the 3279, as c3270 displays them; light gray and
    # brown are not grey and orange, which c3270 may display as black and
    # red.
    assert style(u'x', features=COLOR16) == (letter, 0xF7, 0, 0)
    assert style(u'x', bold=True, features=COLOR16) == (letter, 0xFF, 0, 0)
    assert style(u'x', 'brown', features=COLOR16) == (letter, 0xF6, 0, 0)
    assert style(u'x', 'green', features=COLOR16) == (letter, 0xFC, 0, 0)
    assert style(u'x', 'green', bold=True, features=COLOR16) == (
        letter, 0xF4, 0, 0)
    assert style(u'x', 'brightblue', features=COLOR16) == (
        letter, 0xF1, 0, 0)
    assert style(u'x', 'blue', features=COLOR16) == (letter, 0xF9, 0, 0)
    assert style(u'x', 'magenta', features=COLOR16) == (letter, 0xFB, 0, 0)
    assert style(u'x', 'cyan', features=COLOR16) == (letter, 0xFD, 0, 0)
    assert style(u'x', 'cyan', bold=True, features=COLOR16) == (
        letter, 0xF5, 0, 0)
    assert style(u'x', 'black', bold=True, features=COLOR16) == (
        letter, 0xF9, 0, 0)
    # no color is grey, nor orange.
    assert not set(val for pair in tn3270.PALETTE16.values()
                   for val in pair) & {0xFA, 0xFE}
    # a bar, yellow on blue, and black on cyan.
    assert style(u'x', 'yellow', 'blue', bold=True, features=COLOR16) == (
        letter, 0xF6, 0, 0xF9)
    assert style(u'x', 'black', 'cyan', features=COLOR16) == (
        letter, 0xF8, 0, 0xFD)
    # a block is a reverse video blank of its color, or of its background
    # color when black.
    assert style(u'█', 'red') == (0x40, COLORS['red'], HL_REVERSE, 0)
    assert style(u'▄', 'black', 'cyan') == (
        0x40, COLORS['cyan'], HL_REVERSE, 0)
    assert style(u'█', 'black') == BLANK
    # and of a basic terminal, of '#', without color.
    assert style(u'█', 'red', features=BASIC) == (
        to_ebcdic(u'#', 'bracket'), 0, 0, 0)
    assert style(u'x', 'yellow', 'blue', features=BASIC) == (letter, 0, 0, 0)
    # the cursor of the bbs is underscored.
    assert style_cell(u'x', 'default', 'default', False, False, False,
                      False, True, COLOR)[2] == HL_UNDERSCORE


class Display(object):

    """
    Interprets the writes of the server, as a 3270 terminal would.

    As x3270 does, by default.  As DX3270 does, when ``carry``: extended
    attributes set by SA orders are not reset by a write command, a field
    begun makes its attributes those of the characters that follow, and
    an SA order of type 0, resetting them all, is ignored; and characters
    are displayed by the attribute of the field last begun where they were
    written, rather than of the field they are of.
    """

    def __init__(self, rows=24, columns=80, codepage='bracket', carry=False):
        self.rows, self.columns = rows, columns
        self.codepage = codepage
        self.carry = carry
        self.buffer = [u'\x00'] * (rows * columns)
        self.styles = [None] * (rows * columns)
        self.fields = {}
        self.cursor = 0
        self.alarm = False
        self.style = {}
        self.attrs = [0] * (rows * columns)
        self.attr = 0

    def write(self, record):
        """ Interpret one record of a write command. """
        command, wcc, data = record[0], record[1], record[2:]
        assert command in (CMD_W, CMD_EW, CMD_EWA), hex(command)
        if command != CMD_W:
            self.buffer = [u'\x00'] * (self.rows * self.columns)
            self.styles = [None] * (self.rows * self.columns)
            self.fields = {}
            self.cursor = 0
            self.attrs = [0] * (self.rows * self.columns)
            self.attr = 0
        self.alarm = bool(wcc & 0x04)
        if command != CMD_W or not self.carry:
            self.style = {}
        addr, pos, style = 0, 0, self.style
        while pos < len(data):
            order = data[pos]
            if order == ORDER_SBA:
                addr = decode_address(data[pos + 1], data[pos + 2])
                pos += 3
            elif order in (0x1D, 0x29):
                if order == 0x1D:
                    self.fields[addr] = {0xC0: data[pos + 1]}
                    pos += 2
                else:
                    count = data[pos + 1]
                    pairs = data[pos + 2:pos + 2 + count * 2]
                    self.fields[addr] = dict(zip(pairs[::2], pairs[1::2]))
                    pos += 2 + count * 2
                self.buffer[addr] = u'\x00'
                self.attr = self.fields[addr][0xC0]
                if self.carry:
                    style.clear()
                    style.update((key, val) for key, val in
                                 self.fields[addr].items() if key != 0xC0)
                addr += 1
            elif order == 0x28:
                if data[pos + 1] != 0:
                    style[data[pos + 1]] = data[pos + 2]
                    if not data[pos + 2]:
                        del style[data[pos + 1]]
                elif not self.carry:
                    style.clear()
                pos += 3
            elif order == 0x13:
                self.cursor = addr
                pos += 1
            elif order in (0x3C, 0x12):
                stop = decode_address(data[pos + 1], data[pos + 2])
                char = (u'\x00' if order == 0x12 else
                        data[pos + 3:pos + 4].decode('cp037'))
                while addr != stop:
                    self.buffer[addr] = char
                    if order == 0x3C:
                        self.styles[addr] = dict(style)
                        self.attrs[addr] = self.attr
                    addr = (addr + 1) % len(self.buffer)
                pos += 3 if order == 0x12 else 4
            else:
                self.buffer[addr] = from_ebcdic(bytes((order,)),
                                                self.codepage) or u'?'
                self.styles[addr] = dict(style)
                self.attrs[addr] = self.attr
                addr, pos = addr + 1, pos + 1

    def shown(self, addr):
        """
        Return (character, attributes) displayed at buffer ``addr``.

        The color of a character not set by an SA order is that of its
        field, or of the base colors of its field attribute.  The color of
        a blank, not reversed nor of a background, is not shown.
        """
        if addr in self.fields:
            return u' ', {}
        attr, style = 0, {}
        for pos in range(addr, addr - len(self.buffer), -1):
            if pos % len(self.buffer) in self.fields:
                field = self.fields[pos % len(self.buffer)]
                attr = field[0xC0]
                style = dict((key, val) for key, val in field.items()
                             if key != 0xC0)
                break
        if self.carry:
            attr, style = self.attrs[addr], {}
        style.update(self.styles[addr] or {})
        style = dict((key, val) for key, val in style.items() if val)
        if 0x42 not in style:
            # base colors: protected or not, intensified or not.
            style[0x42] = {(0, 0): 0xF4, (0, 1): 0xF2, (1, 0): 0xF1,
                           (1, 1): 0xF7}[bool(attr & 0x20),
                                         (attr & 0x0C) == 0x08]
        char = self.buffer[addr].replace(u'\x00', u' ')
        if (attr & 0x0C) == 0x0C:
            char = u' '
        if char == u' ' and style.get(0x41) != HL_REVERSE and not style.get(
                0x45):
            style.pop(0x42, None)
        return char, style

    def line(self, row):
        """ Return text of ``row``, nulls as blanks. """
        start = row * self.columns
        return u''.join(self.buffer[start:start + self.columns]).replace(
            u'\x00', u' ')


def read_records(sock, timeout=1.0):
    """ Return records received from the server within ``timeout``. """
    data = bytearray()
    sock.settimeout(0.1)
    stime = time.time()
    while time.time() - stime < timeout:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            if data.endswith(bytes((IAC, EOR_MARK))):
                break
            continue
        if not chunk:
            break
        data.extend(chunk)
    records, record, pos = [], bytearray(), 0
    while pos < len(data):
        if data[pos] == IAC:
            if data[pos + 1] == IAC:
                record.append(IAC)
            elif data[pos + 1] == EOR_MARK:
                records.append(bytes(record))
                record = bytearray()
            pos += 2
            continue
        record.append(data[pos])
        pos += 1
    return records


def send_record(sock, record):
    sock.sendall(record.replace(b'\xff', b'\xff\xff') +
                 bytes((IAC, EOR_MARK)))


@pytest.fixture
def pair(cfg):
    """ A TN3270Client, set up as a 24x80 color terminal, and its socket. """
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    client = TN3270Client(ours, ('127.0.0.1', 3270))
    client.setup(24, 80, 'bracket', colors=7, highlight=True,
                 background=False)
    client.ANSWER_IDLE = 0.05
    yield client, theirs
    ours.close()
    theirs.close()


def draw(client, sock, display):
    """ Draw the screen of ``client``, interpreting it by ``display``. """
    if client.send_ready():
        client.send()
    for record in read_records(sock, timeout=0.3):
        display.write(record)


def receive(client, sock, record):
    """ Send a record from the terminal, received by ``client``. """
    send_record(sock, record)
    stime = time.time()
    while not client.recv_ready():
        assert time.time() - stime < 2
        time.sleep(0.01)
    client.socket_recv()


def enter(client, sock, aid, text=u'', cursor=None, fields=None):
    """
    Press attention key ``aid``, having typed ``text`` as a command.

    ``fields`` are text typed at other buffer positions, by position.
    """
    record = bytes((aid,)) + encode_address(
        client.input_addr if cursor is None else cursor)
    fields = dict(fields or {})
    if text:
        fields[client.input_addr] = text
    for addr, typed in sorted(fields.items()):
        record += bytes((ORDER_SBA,)) + encode_address(addr)
        record += typed.encode('cp037')
    receive(client, sock, record)


def settle(client, sock, display=None):
    """ Wait until the bbs has answered an attention key, and draw. """
    records = []
    stime = time.time()
    while client.hold_time is not None:
        assert time.time() - stime < 3
        time.sleep(client.ANSWER_IDLE)
        if client.send_ready():
            client.send()
        records.extend(read_records(sock, timeout=0.1))
    if client.send_ready():
        client.send()
    records.extend(read_records(sock, timeout=0.1))
    if display:
        for record in records:
            display.write(record)
    return records


def test_screen_drawn(pair):
    client, sock = pair
    display = Display()
    client.send_unicode(u'\x1b[2J\x1b[H\x1b[1;33;44m Board \x1b[m\r\n'
                        u'hello [world]\r\n\x1b[31mred')
    client.repaint_pending = True
    draw(client, sock, display)
    assert display.line(0).startswith(u' Board ')
    assert display.styles[1] == {0x42: COLORS['blue'], 0x41: HL_REVERSE}
    assert display.line(1).startswith(u'hello [world]')
    assert display.styles[0x50] == {0x42: COLORS['white']}
    assert display.line(2).startswith(u'red')
    assert display.styles[160][0x42] == COLORS['red']
    # the input row, and its cursor.
    assert display.line(23).startswith(u' Command ===> ')
    assert display.cursor == client.input_addr == 23 * 80 + 15
    assert client.input_addr - 1 in display.fields
    # changes are written, by write commands, without erasing the screen.
    client.send_unicode(u'\x1b[2;1Hgoodbye')
    time.sleep(client.RENDER_IDLE)
    draw(client, sock, display)
    assert display.line(1).startswith(u'goodbyeworld]')
    assert display.line(0).startswith(u' Board ')
    # nothing more to draw.
    time.sleep(client.RENDER_IDLE)
    assert not client.send_ready()


def test_keys_received(pair):
    client, sock = pair
    display = Display()
    client.repaint_pending = True
    draw(client, sock, display)
    enter(client, sock, AID_PF[2])
    assert client.get_input() == b'\x1b'
    # the input field is erased, and the keyboard locked until the bbs
    # has answered.
    client.send()
    records = read_records(sock, timeout=0.1)
    assert records[0][0] == CMD_W
    assert not records[0][1] & 0x02
    assert tn3270.ORDER_EUA in records[0]
    assert settle(client, sock)[-1][1] & 0x02
    enter(client, sock, AID_PF[3], u'y')
    assert client.get_input() == b'y'
    settle(client, sock)
    enter(client, sock, AID_ENTER)
    assert client.get_input() == b'\r'
    settle(client, sock)
    # what is typed is encoded as the bbs encodes its output.
    client.send_unicode(u'', encoding='cp437')
    enter(client, sock, AID_PF[3], u'caf\xe9')
    assert client.get_input() == b'caf\x82'


def test_return_follows_a_line(pair):
    client, sock = pair
    client.send_unicode(u'Login: ')
    client.repaint_pending = True
    draw(client, sock, Display())
    enter(client, sock, AID_ENTER, u'biff')
    assert client.get_input() == b'biff'
    # the bbs displays what was typed, as a prompt of a line does: it is
    # followed by a return key.
    client.send_unicode(u'biff')
    settle(client, sock)
    assert client.get_input() == b'\r'


def test_return_withheld_from_a_key(pair):
    client, sock = pair
    client.send_unicode(u'Quit? ')
    client.repaint_pending = True
    draw(client, sock, Display())
    enter(client, sock, AID_ENTER, u'y')
    assert client.get_input() == b'y'
    # the bbs takes it as a key of a command: no return key follows, which
    # it would take as another.
    client.send_unicode(u'\x1b[2J\x1b[Hgoodbye')
    settle(client, sock)
    assert client.get_input() == b''


def test_return_of_password(pair):
    client, sock = pair
    client.send_unicode(u'Password: ')
    client.repaint_pending = True
    draw(client, sock, Display())
    enter(client, sock, AID_ENTER, u'secret')
    assert client.get_input() == b'secret\r'


def test_mouse_click(pair):
    client, sock = pair
    client.repaint_pending = True
    draw(client, sock, Display())
    # without mouse reporting, ENTER is the return key, wherever the cursor.
    enter(client, sock, AID_ENTER, cursor=5 * 80 + 10)
    assert client.get_input() == b'\r'
    settle(client, sock)
    # the bbs asks whether clicks may be reported, and asks for them.
    client.send_unicode(u'\x1b[?1000$p\x1b[?2026$p')
    assert client.get_input() == b'\x1b[?1000;2$y\x1b[?2026;0$y'
    client.send_unicode(u'\x1b[?1006;1000h\x1b[?1000$p')
    assert client.get_input() == b'\x1b[?1000;1$y'
    enter(client, sock, AID_ENTER, cursor=5 * 80 + 10)
    assert client.get_input() == b'\x1b[<0;11;6M'
    settle(client, sock)
    # not of the input row, or when something was typed.
    enter(client, sock, AID_ENTER)
    assert client.get_input() == b'\r'
    settle(client, sock)
    enter(client, sock, AID_PF[3], u'x', cursor=5 * 80 + 10)
    assert client.get_input() == b'x'


def test_input_fields(pair):
    client, sock = pair
    display = Display()
    client.send_unicode(u'\x1b[2J\x1b[H Title\r\nfirst row\r\nsecond')
    client.send_unicode(u'\x1b]3270;fields;1;3\x07\x1b[3;7H')
    draw(client, sock, display)
    # each row's field begins by an attribute at the end of the row above.
    for row in (1, 2, 3):
        assert not display.fields[row * 80 - 1][0xC0] & 0x20
    assert display.fields[4 * 80 - 1][0xC0] & 0x20
    assert display.line(1).startswith(u'first row')
    # blanks of fields are nulls, so that characters may be inserted.
    assert display.buffer[80 + 20] == u'\x00'
    assert display.buffer[4 * 80 + 20] == u' '
    # the cursor is the bbs's, in a field.
    assert display.cursor == 2 * 80 + 6
    # what is typed in fields is the event of the attention key, rather
    # than keystrokes.
    enter(client, sock, AID_ENTER, cursor=2 * 80 + 9,
          fields={2 * 80: u'second row'})
    assert client.get_input() == b''
    assert client.pop_events() == [('tn3270', {
        'aid': 'enter', 'text': u'', 'fields': {2: u'second row'},
        'cursor': (2, 9)})]
    settle(client, sock)
    enter(client, sock, AID_PF[14], u'save')
    assert client.pop_events() == [('tn3270', {
        'aid': 'pf3', 'text': u'save', 'fields': {}, 'cursor': None})]
    settle(client, sock)
    # in input fields, PF1 is the bbs's, rather than the page of keys.
    enter(client, sock, AID_PF[0])
    assert client.pop_events()[0][1]['aid'] == 'pf1'
    assert not client.help_pending
    settle(client, sock)
    # no longer.
    client.send_unicode(u'\x1b]3270;fields\x07')
    draw(client, sock, display)
    assert set(display.fields) == {23 * 80, client.input_addr - 1,
                                   24 * 80 - 1}
    enter(client, sock, AID_ENTER)
    assert client.get_input() == b'\r'
    assert client.pop_events() == []


def test_help_and_repaint(pair):
    client, sock = pair
    display = Display()
    client.send_unicode(u'bbs screen')
    client.repaint_pending = True
    draw(client, sock, display)
    enter(client, sock, AID_PA1)
    assert client.get_input() == b''
    draw(client, sock, display)
    assert any(u'PF3' in display.line(row) for row in range(24))
    assert not display.line(0).startswith(u'bbs screen')
    # output of the bbs is not drawn over the help page,
    client.send_unicode(u'\r\nmore')
    time.sleep(client.RENDER_IDLE)
    draw(client, sock, display)
    assert any(u'PF3' in display.line(row) for row in range(24))
    # until any key is pressed, which is not sent to the bbs.
    enter(client, sock, AID_ENTER, u'x')
    assert client.get_input() == b''
    draw(client, sock, display)
    assert display.line(0).startswith(u'bbs screen')
    assert display.line(1).startswith(u'more')
    # PF1 displays it, too, as ISPF's HELP.
    enter(client, sock, AID_PF[0], u'typed')
    assert client.get_input() == b''
    draw(client, sock, display)
    assert any(u'PF3  = END' in display.line(row) for row in range(24))
    enter(client, sock, AID_ENTER)
    draw(client, sock, display)
    # PA2 draws the screen again.
    enter(client, sock, AID_PA2)
    client.send()
    records = read_records(sock, timeout=0.3)
    assert records[0][0] == CMD_EW


def test_password_hidden(pair):
    # the input row is labeled as of the prompt, and does not display a
    # password: of x3270, by its field, and of DX3270, by the attribute of
    # where each character is typed, which is the field's.
    client, sock = pair
    displays = Display(), Display(carry=True)

    def update(text):
        client.send_unicode(text)
        time.sleep(client.RENDER_IDLE)
        if client.send_ready():
            client.send()
        for record in read_records(sock, timeout=0.3):
            for display in displays:
                display.write(record)
    client.repaint_pending = True
    update(u'Hello\r\n')
    assert displays[0].line(23).startswith(u' Command ===> ')
    update(u':: Login: ')
    assert displays[0].line(23).startswith(u' Login ===> ')
    field = displays[0].fields[client.input_addr - 1]
    assert field[0xC0] & 0x0C != FA_NONDISPLAY
    update(u'biff\r\n:: Password: ')
    assert displays[0].line(23).startswith(u' Password ===> ')
    field = displays[0].fields[client.input_addr - 1]
    assert field[0xC0] == encode_attr(FA_NONDISPLAY)
    for addr in range(client.input_addr, 24 * 80 - 1):
        assert displays[1].attrs[addr] == encode_attr(FA_NONDISPLAY)
    update(u'\r\nWelcome, biff.\r\n')
    assert displays[0].line(23).startswith(u' Command ===> ')
    assert displays[1].attrs[client.input_addr] & 0x0C != FA_NONDISPLAY


def test_alarm(pair):
    client, sock = pair
    display = Display()
    client.repaint_pending = True
    draw(client, sock, display)
    assert not display.alarm
    client.send_unicode(u'\a')
    time.sleep(client.RENDER_IDLE)
    draw(client, sock, display)
    assert display.alarm


def test_answers_queries_of_bbs(pair):
    client, _ = pair
    client.send_unicode(u'\x1b[3;5H\x1b[6n')
    assert client.get_input() == b'\x1b[3;5R'


def test_malformed_sequence(pair):
    client, sock = pair
    display = Display()
    # pyte raises on this sequence; drawing continues after it.
    client.send_unicode(u'ab\x1b[0;40 Dcd')
    client.send_unicode(u'\r\nnext')
    client.repaint_pending = True
    draw(client, sock, display)
    assert display.line(1).startswith(u'next')


class FakeTerminal(object):

    """ The far end of a socket, answering negotiation as a 3270 would. """

    def __init__(self, sock, ttype, reply=None):
        self.sock = sock
        self.ttype = ttype
        self.reply = reply
        self.records = []
        self.data = bytearray()

    def pump(self):
        """ Answer what has been received. """
        self.sock.setblocking(False)
        try:
            self.data.extend(self.sock.recv(65536))
        except BlockingIOError:
            return
        data, out = self.data, bytearray()
        record = bytearray()
        pos = 0
        while pos + 1 < len(data):
            if data[pos] != IAC:
                record.append(data[pos])
                pos += 1
                continue
            cmd = data[pos + 1]
            if cmd == IAC:
                record.append(IAC)
                pos += 2
            elif cmd in (DO, WILL) and pos + 2 < len(data):
                opt = data[pos + 2]
                if self.ttype.startswith('IBM') or opt == TTYPE:
                    out += bytes((IAC, WILL if cmd == DO else DO, opt))
                else:
                    out += bytes((IAC, WONT if cmd == DO else DONT, opt))
                pos += 3
            elif cmd == SB:
                end = data.find(bytes((IAC, SE)), pos)
                if end < 0:
                    break
                out += bytes((IAC, SB, TTYPE, 0)) + self.ttype.encode()
                out += bytes((IAC, SE))
                pos = end + 2
            elif cmd == EOR_MARK:
                self.records.append(bytes(record))
                if bytes(record) == QUERY and self.reply:
                    out += self.reply.replace(b'\xff', b'\xff\xff') + bytes(
                        (IAC, EOR_MARK))
                record = bytearray()
                pos += 2
            else:
                break
        del self.data[:pos - len(record)]
        if out:
            self.sock.sendall(out)


def negotiate(cfg, ttype, reply=None):
    """ Negotiate with a fake terminal, returning client and terminal. """
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    client = TN3270Client(ours, ('127.0.0.1', 3270))
    terminal = FakeTerminal(theirs, ttype, reply)
    connect = ConnectTN3270(client)
    connect.TIME_NEGOTIATE = connect.TIME_QUERY = 1.0
    result = {}

    def run():
        try:
            connect.negotiate()
        except ValueError as err:
            result['error'] = err
            connect.not_3270()

    thread = threading.Thread(target=run)
    thread.start()
    while thread.is_alive():
        terminal.pump()
        if client.recv_ready():
            client.socket_recv()
        time.sleep(0.01)
    time.sleep(0.05)
    terminal.pump()
    return client, terminal, result.get('error')


def test_negotiate_extended(cfg):
    client, terminal, error = negotiate(cfg, 'IBM-3279-5-E', QUERY_REPLY)
    assert error is None
    assert (client.rows, client.columns) == (27, 132)
    assert client.features == ('bracket', 16, True, True)
    assert client.env['LINES'] == '26' and client.env['COLUMNS'] == '132'
    assert client.env['TERM'] == 'xterm-256color'
    assert client.remote >= {EOR, 0} and client.local >= {EOR, 0}
    # the help page is displayed, by erase/write alternate.
    assert terminal.records[0] == QUERY
    assert terminal.records[-1][0] == CMD_EWA
    assert client.showing_help


def test_negotiate_dx3270(cfg):
    # DX3270, reporting no code page, is of true cp037, its brackets
    # where those of the x3270 family are not.
    client, terminal, error = negotiate(cfg, 'IBM-3279-2-E', DX3270_REPLY)
    assert error is None
    assert client.features == ('cp037', 7, True, False)
    # unless configured.
    cfg.set('tn3270', 'codepage', 'bracket')
    client, terminal, error = negotiate(cfg, 'IBM-3279-2-E', DX3270_REPLY)
    assert client.features == ('bracket', 7, True, False)


def test_negotiate_without_query(cfg):
    client, terminal, error = negotiate(cfg, 'IBM-3278-4')
    assert error is None
    assert (client.rows, client.columns) == (43, 80)
    assert client.features == ('bracket', 0, False, False)
    assert QUERY not in terminal.records


def test_not_a_3270(cfg):
    client, terminal, error = negotiate(cfg, 'xterm')
    assert 'not a 3270' in str(error)
    assert b'This port is for 3270 terminals' in terminal.data


@pytest.mark.parametrize('colors', [7, 0])
def test_terminals_that_carry_attributes(pair, colors):
    # DX3270 keeps attributes where x3270 resets them, and displays each
    # character by the field last begun where it was written: the screen is
    # displayed the same by either, of a terminal of colors, or of none.
    client, sock = pair
    if not colors:
        client.setup(24, 80, 'bracket', colors=0, highlight=False,
                     background=False)
    displays = Display(), Display(carry=True)
    screens = [
        u'\x1b[2J\x1b[H\x1b[1;37;44m Title bar \x1b[m\r\n'
        u'\x1b[7mselected\x1b[m plain\r\n\x1b[4munder\x1b[m\r\nnext',
        u'\x1b[2;1H\x1b[32mgreen\x1b[m\x1b[3;1Hplain \x1b[7mreverse\x1b[m',
        u'\x1b[4;1H\x1b[1;33;41mbar\x1b[m\x1b[5;1Hafter\r\nPassword: ',
        u'\x1b[7;1Hmore text, of the row after the password',
    ]
    client.repaint_pending = True
    for screen in screens:
        client.send_unicode(screen)
        time.sleep(client.RENDER_IDLE)
        if client.send_ready():
            client.send()
        for record in read_records(sock, timeout=0.3):
            for display in displays:
                display.write(record)
    for addr in range(23 * 80):
        assert displays[0].shown(addr) == displays[1].shown(addr), (
            divmod(addr, 80), displays[0].shown(addr), displays[1].shown(addr))
    assert displays[1].line(6).startswith(u'more text')
    if colors:
        assert displays[1].shown(80) == (u'g', {0x42: COLORS['green']})
        assert displays[1].shown(2 * 80 + 6) == (
            u'r', {0x42: COLORS['white'], 0x41: HL_REVERSE})
        assert displays[1].shown(3 * 80) == (
            u'b', {0x42: COLORS['red'], 0x41: HL_REVERSE})
        assert displays[1].shown(6 * 80) == (u'm', {0x42: COLORS['white']})
    else:
        # all of the bbs's screen is of a protected field: blue.
        assert displays[1].shown(6 * 80) == (u'm', {0x42: COLORS['blue']})


def test_query_reply_of_dx3270():
    # DX3270 omits the identifier of query replies, and reports 7 colors.
    result = parse_query_reply(DX3270_REPLY)
    assert result['colors'] == 7
    assert 0x87 in result['codes']
    # nor its code page, or name.
    assert result['codepage'] is None and result['name'] is None


def test_choose_codepage():
    def reply(codepage, name):
        return {'codepage': codepage, 'name': name}
    # the x3270 family reports 37 of 'bracket', its default.
    assert tn3270.choose_codepage('auto', reply(37, u'x3270')) == 'bracket'
    # others are of the code page reported, as of true cp037, and as
    # DX3270, which reports none.
    assert tn3270.choose_codepage('auto', reply(37, None)) == 'cp037'
    assert tn3270.choose_codepage('auto', reply(None, None)) == 'cp037'
    assert tn3270.choose_codepage('auto', reply(500, u'x3270')) == 'cp500'
    assert tn3270.choose_codepage('auto', reply(1140, None)) == 'cp1140'
    # cp1047, which python lacks, is of brackets as 'bracket'.
    assert tn3270.choose_codepage('auto', reply(1047, None)) == 'bracket'
    assert tn3270.choose_codepage('auto', reply(9999, None)) == 'cp037'
    # of a terminal not asked, or not answering.
    assert tn3270.choose_codepage('auto', None) == 'bracket'
    assert tn3270.choose_codepage(None, None) == 'bracket'
    # unless configured.
    assert tn3270.choose_codepage('cp273', reply(37, u'x3270')) == 'cp273'
    assert tn3270.choose_codepage('bracket', reply(None, None)) == 'bracket'


def test_logon_screen(cfg):
    # a logon screen, as of VTAM, is displayed on connecting.
    client, terminal, error = negotiate(cfg, 'IBM-3279-2-E', QUERY_REPLY)
    display = Display(rows=27, columns=132)
    display.write(terminal.records[-1])
    text = u'\n'.join(display.line(row) for row in range(27))
    # the name of the bbs, in large letters, of itself.
    assert u'X   X     /  888  4   4' in text
    assert u'SYSTEM . . . . : X/84' in text
    assert u'TERMINAL . . . : ' + client.terminal_id in text
    assert u'IBM-3279-2-E  27 X 132' in text
    assert u'PF3  = END' in text
    assert u'PRESS ENTER TO LOG ON TO X/84' in text
    assert client.terminal_id.startswith(u'X84T')
    # PF1 displays the keys, rather than leaving it.
    client.ANSWER_IDLE = 0.05
    send_record(terminal.sock, bytes((AID_PF[0],)) + encode_address(0))
    while not client.recv_ready():
        time.sleep(0.01)
    client.socket_recv()
    assert client.help_pending == 'help'


def test_logon_screen_logo_aligned(cfg):
    # the rows of the large letters, of differing lengths, are aligned.
    cfg.set('system', 'bbsname', 'bttfbbs')
    client, terminal, error = negotiate(cfg, 'IBM-3279-2-E', QUERY_REPLY)
    display = Display(rows=27, columns=132)
    display.write(terminal.records[-1])
    logo = tn3270.block_letters(u'bttfbbs', 128)
    left = (132 - max(len(line) for line in logo)) // 2
    for idx, line in enumerate(logo):
        assert display.line(1 + idx)[left:].rstrip() == line


def test_block_letters():
    assert tn3270.block_letters(u'hi', 80) == [
        u'H   H IIIII', u'H   H   I', u'HHHHH   I', u'H   H   I',
        u'H   H IIIII']
    # too wide, the text as it is.
    assert tn3270.block_letters(u'a' * 14, 80) == [u'A' * 14]
