"""
TN3270 server for x/84.

IBM 3270 terminals, and the emulators of them, such as x3270, c3270 and
wc3270, do not send each key as it is pressed: the 3270 displays a form,
which is filled in locally and sent only when an "attention" key, such as
ENTER or a PF key, is pressed.  The bbs is written for character terminals,
so this server keeps a virtual ANSI terminal (by pyte_) for each session,
and draws its screen on the 3270, its colors and characters translated to
those of the 3270 and its EBCDIC code page.

A logon screen, as of VTAM, is displayed on connecting.  The bottom row of
the 3270 is a field for typing, after ``Command ===>`` as of ISPF, and the
bbs is given the rows above it.  What is typed there is sent to the bbs by
ENTER, followed by Return, or by a PF key, followed by the key that the PF
key stands for (see :data:`PF_KEYS`), as ISPF names them: PF3 END and PF12
CANCEL are Escape, PF7 UP and PF8 DOWN are Page Up and Page Down, and PF4
sends only what was typed.  PF1 HELP and PA1 display a page describing
them; CLEAR and PA2 draw the screen again.

To enable, add to default.ini::

    [tn3270]
    enabled = yes

The following options are available, but not required:

- ``addr``, ``port``: where to listen, ``127.0.0.1`` port ``3270`` by
  default.
- ``tls_port``: a port to listen on for terminals connecting by TLS, such
  as with the ``L:`` prefix in the x3270 family, none by default.  It
  requires ``cert`` and ``key``: paths to the certificate, with its chain,
  and to its private key.
- ``codepage``: the EBCDIC code page of terminals, by default ``auto``:
  that a terminal reports, when it answers a query, but ``bracket`` for
  the x3270 family, and ``cp037`` for terminals that report none.
  ``bracket`` is of the x3270 family: cp037 (U.S. and Canada), of brackets
  moved.  Others are named as by python: ``cp037``, ``cp273`` (Germany),
  ``cp500`` (international), ``cp1140`` (cp037, with the euro sign).
- ``term``: terminal type of the bbs session, ``xterm-256color`` by
  default.
- ``background``: whether characters on a colored background are displayed
  in their colors on it, ``yes``, or in reverse video, of the background's
  color, ``no``.  By default, ``auto``, they are by terminals of 16 colors,
  as the x3270 family; some terminals refuse such a screen.

The terminal type reported by the terminal (such as ``IBM-3279-2-E``)
decides the size of its screen, unless it answers a query of its size,
which a terminal type ending ``-E`` (an "extended" one) is asked.  Colors
and reverse video are displayed only by terminals that answer it: the 16
colors of the x3270 family, nearly one for each ANSI color, normal and
bright, or the 7 of a 3279.

This server requires pyte, installed by the ``tn3270`` extra::

    pip install x84[tn3270]

.. _pyte: https://github.com/selectel/pyte
"""
# std imports
import codecs
import functools
import itertools
import logging
import os
import re
import select
import ssl
import threading
import time
import unicodedata

# local
from x84.bbs.exception import Disconnected
from x84.client import BaseClient, BaseConnect
from x84.server import BaseServer, get_ssl_context
from x84.terminal import spawn_client_session

try:
    import pyte
except ImportError as err:
    raise ImportError('The tn3270 server requires pyte, by: '
                      'pip install x84[tn3270] ({0})'.format(err))

# telnet commands and options
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
EOR_MARK = 239
BINARY, TTYPE, EOR = 0, 24, 25
TTYPE_IS, TTYPE_SEND = 0, 1

# 3270 commands
CMD_W, CMD_EW, CMD_EWA, CMD_WSF = 0xF1, 0xF5, 0x7E, 0xF3

# write control character bits
WCC_ALARM, WCC_RESTORE, WCC_RESET_MDT = 0x04, 0x02, 0x01

# 3270 orders
ORDER_SF, ORDER_SFE, ORDER_SBA, ORDER_SA = 0x1D, 0x29, 0x11, 0x28
ORDER_IC, ORDER_RA, ORDER_EUA, ORDER_GE = 0x13, 0x3C, 0x12, 0x08

# field attributes
FA_PROTECTED, FA_NUMERIC, FA_INTENSIFIED = 0x20, 0x10, 0x08
FA_NONDISPLAY, FA_MDT = 0x0C, 0x01

#: protected and numeric: the cursor skips over the field.
FA_SKIP = FA_PROTECTED | FA_NUMERIC

# extended attribute types, of orders SA and SFE
XA_ALL, XA_FIELD, XA_HIGHLIGHT, XA_COLOR, XA_BACKGROUND = (
    0x00, 0xC0, 0x41, 0x42, 0x45)

# extended highlighting
HL_DEFAULT, HL_BLINK, HL_REVERSE, HL_UNDERSCORE = 0x00, 0xF1, 0xF2, 0xF4

# colors
COLOR_DEFAULT = 0x00
COLORS = {
    'blue': 0xF1, 'red': 0xF2, 'magenta': 0xF3, 'green': 0xF4,
    'cyan': 0xF5, 'yellow': 0xF6, 'white': 0xF7,
}

#: colors of the ANSI colors, (normal, bright), at a terminal of 16 colors,
#: as the x3270 family: the extended colors (deep blue, purple, pale green,
#: pale turquoise) are normal, and those of the 3279 are bright, as c3270
#: displays them at a terminal of 16 colors.  It displays grey as black,
#: and orange as red, at a terminal of 8 colors (or of 16): so light gray is
#: white, and brown is yellow.  Dark gray, bright black, is deep blue, and
#: black is displayed only on a background.
PALETTE16 = {
    'black': (0xF8, 0xF9), 'red': (0xF2, 0xF2), 'green': (0xFC, 0xF4),
    'yellow': (0xF6, 0xF6), 'blue': (0xF9, 0xF1), 'magenta': (0xFB, 0xF3),
    'cyan': (0xFD, 0xF5), 'white': (0xF7, 0xFF),
}

#: and of the 7 colors of a 3279, of which bright and normal are the same.
PALETTE8 = dict((name, (COLORS[name],) * 2) for name in COLORS)
PALETTE8['black'] = (0xF8, COLORS['blue'])

# attention identifiers (AID) of keys
AID_STRUCTURED_FIELD = 0x88
AID_ENTER, AID_CLEAR = 0x7D, 0x6D
AID_PA1, AID_PA2, AID_PA3 = 0x6C, 0x6E, 0x6B
AID_PF = (0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9,
          0x7A, 0x7B, 0x7C, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6,
          0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C)

#: keys that send no cursor address or fields, only their AID.
SHORT_AIDS = (AID_CLEAR, AID_PA1, AID_PA2, AID_PA3, 0x6A)

#: what the bbs receives for each PF key, PF1-PF24, after what was typed,
#: as ISPF names them: PF1 HELP, PF3 END, PF7 UP, PF8 DOWN, PF12 CANCEL.
#: PF13-PF24 are the same as PF1-PF12, but PF16 and PF21.  None, of the
#: page describing the keys, rather than keys.
PF_KEYS = (
    None,           # PF1: help
    u'\x7f',        # PF2: backspace
    u'\x1b',        # PF3: end, escape
    u'',            # PF4: what was typed, only
    u'\x1b[A',      # PF5: up
    u'\x1b[B',      # PF6: down
    u'\x1b[5~',     # PF7: page up
    u'\x1b[6~',     # PF8: page down
    u'\x1b[H',      # PF9: home
    u'\x1b[D',      # PF10: left
    u'\x1b[C',      # PF11: right
    u'\x1b',        # PF12: cancel, escape
    None,           # PF13: help
    u'\x7f',        # PF14: backspace
    u'\x1b',        # PF15: end, escape
    u'\t',          # PF16: tab
    u'\x1b[A',      # PF17: up
    u'\x1b[B',      # PF18: down
    u'\x1b[5~',     # PF19: page up
    u'\x1b[6~',     # PF20: page down
    u'\x1b[F',      # PF21: end of line
    u'\x1b[D',      # PF22: left
    u'\x1b[C',      # PF23: right
    u'\x1b',        # PF24: cancel, escape
)

#: numbers of terminals connected, of their names.
TERMINAL_NUMBERS = itertools.count(1)

#: labels of the input row, as of ISPF, and of the prompts of a login and
#: of a password, and the columns they are given.
PROMPT = u'Command ===>'
PROMPT_LOGIN = u'Login ===>'
PROMPT_PASSWORD = u'Password ===>'
PROMPT_WIDTH = max(len(PROMPT), len(PROMPT_LOGIN), len(PROMPT_PASSWORD))

#: a prompt of a login, ending the text left of the bbs's cursor.
RE_LOGIN = re.compile(r'login:\s*$', re.IGNORECASE)

#: 6-bit codes of addresses and of attribute bytes.
CODES = bytes((
    0x40, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
    0xC8, 0xC9, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F,
    0x50, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7,
    0xD8, 0xD9, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F,
    0x60, 0x61, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7,
    0xE8, 0xE9, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F,
    0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7,
    0xF8, 0xF9, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F,
))

#: screen sizes (rows, columns) of 3278 and 3279 models.
MODELS = {'2': (24, 80), '3': (32, 80), '4': (43, 80), '5': (27, 132)}

#: terminal types of 3270 models, such as IBM-3279-2-E.
RE_MODEL = re.compile(r'IBM-327[89]-([2-5])(-E)?$')

# query reply codes
QR_USABLE_AREA, QR_CHARSETS, QR_COLOR, QR_HIGHLIGHT = (
    0x81, 0x85, 0x86, 0x87)
QR_RPQ_NAMES, QR_IMPLICIT_PARTITION = 0xA1, 0xA6

#: code pages reported by terminals of brackets as the x3270 family's, as
#: of Hercules: cp1047, which python lacks.
BRACKET_CODEPAGES = (1047,)

#: write structured field, read partition query.
QUERY = bytes((CMD_WSF, 0x00, 0x05, 0x01, 0xFF, 0x02))

#: characters displayed as a block of their color.
SOLID = u'█▓▀▄▌▐' + u''.join(
    chr(code) for code in range(0x2596, 0x25A0))

#: characters without an EBCDIC equal, and what is displayed instead.
SUBSTITUTES = {
    u'░': u'.', u'▒': u':',
    u'←': u'<', u'→': u'>', u'↑': u'^', u'↓': u'v',
    u'•': u'o', u'●': u'o', u'○': u'o', u'∙': u'.',
    u'■': u'#', u'►': u'>', u'◄': u'<', u'–': u'-',
    u'—': u'-', u'‘': u"'", u'’': u"'", u'“': u'"',
    u'”': u'"', u'…': u'.',
}

#: scripts declare rows of input fields by this sequence: ``fields;top;bottom``
#: of the first and last rows, or ``fields`` of none.  See
#: :meth:`TN3270Client.declare`.
DECLARE = u'\x1b]3270;'
RE_DECLARE = re.compile(u'\x1b\\]3270;([^\x07\x1b]*)(?:\x07|\x1b\\\\)')

#: query of a mode of the terminal (DECRQM), by blessed.
RE_DECRQM = re.compile(u'\x1b\\[\\?(\\d+)\\$p')

#: either, in the order received.
RE_CONTROL = re.compile(u'(?:{0})|(?:{1})'.format(
    RE_DECLARE.pattern, RE_DECRQM.pattern))

#: modes of reporting clicks of the mouse, and of their encoding (SGR).
MOUSE_MODES = (1000, 1006)

#: maximum length of an inbound 3270 record.
RECORD_MAXLEN = 16384

#: maximum length of a telnet subnegotiation.
SB_MAXLEN = 256


def encode_address(pos):
    """ Return 3270 buffer address ``pos`` as two bytes. """
    if pos < 4096:
        return bytes((CODES[(pos >> 6) & 0x3F], CODES[pos & 0x3F]))
    # 14-bit binary address, of screens larger than 4096 positions.
    return bytes(((pos >> 8) & 0x3F, pos & 0xFF))


def decode_address(byte1, byte2):
    """ Return buffer position of 3270 address ``byte1``, ``byte2``. """
    if byte1 & 0xC0 == 0:
        return ((byte1 & 0x3F) << 8) | byte2
    return ((byte1 & 0x3F) << 6) | (byte2 & 0x3F)


def encode_attr(value):
    """ Return field attribute, or write control character, ``value``. """
    return CODES[value & 0x3F]


def parse_ttype(ttype):
    """
    Return ``(rows, columns, extended)`` of 3270 terminal type ``ttype``.

    ``extended`` is whether the terminal may answer a query of its size and
    features: a terminal type ending ``-E``, or ``IBM-DYNAMIC``.
    """
    ttype = (ttype or u'').upper()
    match = RE_MODEL.match(ttype)
    if match:
        rows, columns = MODELS[match.group(1)]
        return rows, columns, bool(match.group(2))
    return 24, 80, ttype == 'IBM-DYNAMIC'


def parse_query_reply(record):
    """
    Parse the answer of a terminal to a read partition query.

    :param bytes record: inbound record, beginning with AID 0x88.
    :returns: dict of ``codes``, the set of query reply codes, ``size``,
        the ``(rows, columns)`` of the terminal, or None, ``colors``, the
        number of colors it displays: 0, 7, or 16, ``codepage``, the
        number of its code page, such as 37, or None, and ``name``, as of
        the emulator, such as ``'x3270'``, or None.
    """
    codes, size, usable, colors = set(), None, None, 0
    codepage, name = None, None
    pos = 1
    while pos + 4 <= len(record):
        length = (record[pos] << 8) | record[pos + 1]
        if length == 0:
            length = len(record) - pos
        if length < 4:
            break
        field = record[pos:pos + length]
        if field[2] == 0x81:
            code, data = field[3], field[4:]
        else:
            # without the identifier of a query reply, as of DX3270.
            code, data = field[2], field[3:]
        codes.add(code)
        if (code == QR_IMPLICIT_PARTITION and len(data) >= 13
                and data[3] == 0x01):
            # alternate width and height of the implicit partition.
            size = ((data[11] << 8) | data[12],
                    (data[9] << 8) | data[10])
        elif code == QR_USABLE_AREA and len(data) >= 6:
            usable = ((data[4] << 8) | data[5],
                      (data[2] << 8) | data[3])
        elif code == QR_COLOR and len(data) >= 2:
            # pairs of color attribute and the color displayed, which
            # is 0 of those that are not, as of a monochrome terminal.
            pairs = data[2:2 + 2 * data[1]]
            shown = set(attr for attr, color in zip(pairs[::2], pairs[1::2])
                        if color)
            colors = (16 if set(range(0xF8, 0x100)) <= shown else
                      7 if set(range(0xF1, 0xF8)) <= shown else 0)
        elif (code == QR_CHARSETS and len(data) >= 9 and data[0] & 0x02
                and data[8] >= 7):
            # descriptors, each ending with its CGCSID, of character set
            # and code page, when present: that of the first, the base set.
            if len(data) >= 9 + data[8]:
                cgcsid = data[9 + data[8] - 4:9 + data[8]]
                codepage = (cgcsid[2] << 8) | cgcsid[3]
        elif code == QR_RPQ_NAMES and len(data) >= 9 and data[8] >= 1:
            # device type and model, and the name, of its length first.
            name = bytes(data[9:8 + data[8]]).decode('cp037', 'replace')
        pos += length
    return {'codes': codes, 'size': size or usable, 'colors': colors,
            'codepage': codepage, 'name': name}


def choose_codepage(configured, reply):
    """
    Return the code page of a terminal, of its query ``reply``.

    :param str configured: option ``codepage``: chosen when other than
        ``auto``.
    :param dict reply: as returned by :func:`parse_query_reply`, or None
        when the terminal was not asked, or did not answer.
    """
    if configured and configured.lower() != 'auto':
        return configured
    if reply is None:
        # most such terminals are of the x3270 family, of no -E model.
        return 'bracket'
    number, x3270 = reply['codepage'], reply['name'] == 'x3270'
    if number in BRACKET_CODEPAGES or (x3270 and number in (37, None)):
        # the x3270 family reports 37 of 'bracket', as of cp037.
        return 'bracket'
    if number is not None:
        try:
            codecs.lookup('cp{0:03d}'.format(number))
        except LookupError:
            pass
        else:
            return 'cp{0:03d}'.format(number)
    # as DX3270, which reports none.
    return 'cp037'


def parse_input(record):
    """
    Parse an inbound record sent by an attention key.

    :param bytes record: inbound record, beginning with its AID.
    :returns: ``(aid, cursor, fields)``, where ``cursor`` is the buffer
        position of the cursor, or None, and ``fields`` is a dict of the
        modified fields sent, by the position of their first character.
    """
    aid = record[0]
    if aid in SHORT_AIDS or len(record) < 3:
        return aid, None, {}
    cursor = decode_address(record[1], record[2])
    fields, addr, pos = {}, None, 3
    while pos < len(record):
        if record[pos] == ORDER_SBA and pos + 2 < len(record):
            addr = decode_address(record[pos + 1], record[pos + 2])
            fields[addr] = bytearray()
            pos += 3
            continue
        if addr is not None and record[pos] != ORDER_GE:
            fields[addr].append(record[pos])
        pos += 1
    return aid, cursor, {key: bytes(val) for key, val in fields.items()}


def keys_of(aid, text):
    """
    Return what the bbs receives for attention key ``aid``.

    :param int aid: attention identifier of the key pressed.
    :param str text: what was typed in the input field.
    :returns: keystrokes, or None when ``aid`` does not send any.
    """
    if aid == AID_ENTER:
        return text + u'\r'
    if aid in AID_PF and PF_KEYS[AID_PF.index(aid)] is not None:
        return text + PF_KEYS[AID_PF.index(aid)]
    return None


def is_help(aid):
    """ Whether attention key ``aid`` displays the page of the keys. """
    return aid == AID_PA1 or (aid in AID_PF and
                              PF_KEYS[AID_PF.index(aid)] is None)


#: the code page of the x3270 family by default, "bracket": cp037, but of
#: brackets where cp037 has Y-acute and diaeresis, and those where it has
#: brackets.
BRACKET = bytes.maketrans(b'\xad\xbd\xba\xbb', b'\xba\xbb\xad\xbd')


def python_codec(codepage):
    """ Return python codec, and byte translation, of 3270 ``codepage``. """
    if codepage == 'bracket':
        return 'cp037', BRACKET
    return codepage, None


def aid_name(aid):
    """ Return name of attention key ``aid``, 'enter' or 'pf1'-'pf12'. """
    if aid == AID_ENTER:
        return 'enter'
    if aid in AID_PF:
        return 'pf{0}'.format(AID_PF.index(aid) % 12 + 1)
    return None


@functools.lru_cache(maxsize=4096)
def to_ebcdic(char, codepage):
    """ Return the EBCDIC byte displaying ``char``, by ``codepage``. """
    if not char:
        # the second cell of a wide character.
        return 0x40
    candidates = [char]
    if char in SUBSTITUTES:
        candidates.append(SUBSTITUTES[char])
    elif u'─' <= char <= u'╿':
        # box drawing.
        name = unicodedata.name(char, u'')
        if u'HORIZONTAL' in name and u'VERTICAL' not in name:
            candidates.append(u'-')
        elif u'VERTICAL' in name and u'HORIZONTAL' not in name:
            candidates.append(u'|')
        else:
            candidates.append(u'+')
    else:
        candidates.extend(unicodedata.normalize('NFKD', char)[:1])
    candidates.append(u'?')
    codec, table = python_codec(codepage)
    for candidate in candidates:
        try:
            encoded = candidate.encode(codec)
        except UnicodeEncodeError:
            continue
        if table:
            encoded = encoded.translate(table)
        # bytes below 0x40 are orders and controls of the 3270, and 0xff
        # is not displayed.
        if len(encoded) == 1 and 0x40 <= encoded[0] < 0xFF:
            return encoded[0]
    return 0x6F


def from_ebcdic(data, codepage):
    """ Return text typed at a 3270, of EBCDIC ``data``. """
    codec, table = python_codec(codepage)
    if table:
        data = data.translate(table)
    return u''.join(char for char in data.decode(codec, 'replace')
                    if char.isprintable())


@functools.lru_cache(maxsize=1024)
def ansi_color(color, bold, default):
    """
    Return ``(name, bright)`` of ANSI color ``color``, as pyte names it.

    ``name`` is of :data:`PALETTE16`.  Bold colors are bright, as at the
    terminals of bulletin boards.  Colors of 256-color and direct-color
    sequences are of their nearest.
    """
    if color == 'default':
        return default, bold
    if color.startswith('bright'):
        color, bold = color[len('bright'):], True
    if color == 'brown':
        color = 'yellow'
    if color in PALETTE16:
        return color, bold
    try:
        red, green, blue = (int(color[idx:idx + 2], 16)
                            for idx in (0, 2, 4))
    except ValueError:
        return default, bold
    peak = max(red, green, blue)
    if peak < 0x40:
        return 'black', False
    # the hue of the color, scaled to its brightest.
    hue = tuple(val * 255 // peak > 0x7F for val in (red, green, blue))
    return {
        (False, False, False): 'black', (True, False, False): 'red',
        (False, True, False): 'green', (True, True, False): 'yellow',
        (False, False, True): 'blue', (True, False, True): 'magenta',
        (False, True, True): 'cyan', (True, True, True): 'white',
    }[hue], peak >= 0xE0


#: a blank cell: its color does not matter.
BLANK = (0x40, None, HL_DEFAULT, COLOR_DEFAULT)


@functools.lru_cache(maxsize=8192)
def style_cell(data, fg, bg, bold, reverse, underscore, blink, cursor,
               features):
    """
    Return how a character of the virtual terminal is displayed.

    :param str data: character.
    :param str fg: foreground color, as pyte names it.
    :param str bg: background color, as pyte names it.
    :param bool bold: whether bold, which of black, is dark gray.
    :param bool reverse: whether displayed in reverse video.
    :param bool underscore: whether underlined.
    :param bool blink: whether blinking.
    :param bool cursor: whether the cursor of the bbs is here.
    :param tuple features: ``(codepage, colors, highlight, background)``,
        the code page of the terminal, the number of colors it displays (0,
        7, or 16), and whether it displays extended highlighting (reverse
        video, underscore, blink), and background colors.
    :returns: ``(byte, color, highlight, background)``: an EBCDIC byte
        and the values of its extended attributes, :data:`BLANK` for a
        blank of no highlighting or background.
    """
    codepage, colors, has_highlight, has_background = features
    palette = PALETTE16 if colors >= 16 else PALETTE8
    black = ('black', False)
    fg = ansi_color(fg, bold, 'white')
    bg = ansi_color(bg, False, 'black')
    if reverse:
        fg, bg = bg, fg
    solid = data in SOLID
    if solid:
        # a block of its color, or when black, a half block of the color
        # of its background.
        block = bg if fg == black else fg
        if block == black:
            return BLANK
        data, fg, bg = u' ', black, block
    highlight, background = HL_DEFAULT, COLOR_DEFAULT
    if bg == black:
        if fg == black:
            # black on black is not displayed.
            return BLANK
        color = palette[fg[0]][fg[1]]
    elif has_background and fg != bg:
        color = palette[fg[0]][fg[1]]
        background = palette[bg[0]][bg[1]]
    elif has_highlight:
        color, highlight = palette[bg[0]][bg[1]], HL_REVERSE
    else:
        # without reverse video, a block is of '#', and text is of its own
        # color, unless black, then of its background's.
        color = (palette[bg[0]][bg[1]] if fg == black
                 else palette[fg[0]][fg[1]])
        if solid:
            data = u'#'
    if highlight == HL_DEFAULT and has_highlight:
        if underscore or cursor:
            highlight = HL_UNDERSCORE
        elif blink:
            highlight = HL_BLINK
    byte = to_ebcdic(data, codepage)
    if byte == 0x40 and highlight == HL_DEFAULT and not background:
        return BLANK
    return (byte, color if colors else COLOR_DEFAULT,
            highlight, background)


def encode_cells(cells, start, end_addr, fill=0x40,
                 kinds=(True, True, False)):
    """
    Return orders and characters displaying ``cells`` at buffer ``start``.

    Terminals differ in the extended attributes in effect where a row
    begins, as of the field that it follows, or of an earlier write; and
    some ignore an SA order of type 0, that resets them all.  So each
    attribute is set as the first cell needs it, and each is reset to its
    default by its own SA order, where the row ends.

    :param list cells: tuples of :func:`style_cell`.
    :param int start: buffer position of the first cell.
    :param int end_addr: buffer position after the last cell.
    :param int fill: character of the blanks ending the row: of an input
        field, nulls, so that characters may be inserted.
    :param tuple kinds: whether the terminal is sent attributes of color,
        highlighting and background color.
    :returns: bytes, beginning with an SBA order.
    """
    types = (XA_COLOR, XA_HIGHLIGHT, XA_BACKGROUND)
    defaults = (COLOR_DEFAULT, HL_DEFAULT, COLOR_DEFAULT)
    # None, of an attribute in effect that is not known.
    current = [None if kind else default
               for kind, default in zip(kinds, defaults)]
    out = bytearray((ORDER_SBA,)) + encode_address(start)

    def apply(values):
        for idx, value in enumerate(values):
            if value is not None and value != current[idx]:
                out.extend((ORDER_SA, types[idx], value))
                current[idx] = value

    # the run of blanks ending the row is erased by one RA order.
    last = len(cells)
    while last and cells[last - 1] == BLANK:
        last -= 1
    for byte, color, highlight, background in cells[:last]:
        apply((color, highlight, background))
        out.append(byte)
    if last < len(cells):
        apply(BLANK[1:])
        if len(cells) - last > 3:
            out += bytes((ORDER_RA,)) + encode_address(end_addr) + bytes(
                (fill,))
        else:
            out += bytes((fill,)) * (len(cells) - last)
    # attributes set by SA last until the end of the write command: reset
    # them, for what follows.
    apply(tuple(default if kind else None
                for kind, default in zip(kinds, defaults)))
    return bytes(out)


#: letters of 5 rows, of the logon screen, as drawn by the letter itself.
BLOCK_LETTERS = {
    'A': (' ### ', '#   #', '#####', '#   #', '#   #'),
    'B': ('#### ', '#   #', '#### ', '#   #', '#### '),
    'C': (' ####', '#    ', '#    ', '#    ', ' ####'),
    'D': ('#### ', '#   #', '#   #', '#   #', '#### '),
    'E': ('#####', '#    ', '#### ', '#    ', '#####'),
    'F': ('#####', '#    ', '#### ', '#    ', '#    '),
    'G': (' ####', '#    ', '#  ##', '#   #', ' ####'),
    'H': ('#   #', '#   #', '#####', '#   #', '#   #'),
    'I': ('#####', '  #  ', '  #  ', '  #  ', '#####'),
    'J': ('  ###', '    #', '    #', '#   #', ' ### '),
    'K': ('#   #', '#  # ', '###  ', '#  # ', '#   #'),
    'L': ('#    ', '#    ', '#    ', '#    ', '#####'),
    'M': ('#   #', '## ##', '# # #', '#   #', '#   #'),
    'N': ('#   #', '##  #', '# # #', '#  ##', '#   #'),
    'O': (' ### ', '#   #', '#   #', '#   #', ' ### '),
    'P': ('#### ', '#   #', '#### ', '#    ', '#    '),
    'Q': (' ### ', '#   #', '# # #', '#  # ', ' ## #'),
    'R': ('#### ', '#   #', '#### ', '#  # ', '#   #'),
    'S': (' ####', '#    ', ' ### ', '    #', '#### '),
    'T': ('#####', '  #  ', '  #  ', '  #  ', '  #  '),
    'U': ('#   #', '#   #', '#   #', '#   #', ' ### '),
    'V': ('#   #', '#   #', '#   #', ' # # ', '  #  '),
    'W': ('#   #', '#   #', '# # #', '## ##', '#   #'),
    'X': ('#   #', ' # # ', '  #  ', ' # # ', '#   #'),
    'Y': ('#   #', ' # # ', '  #  ', '  #  ', '  #  '),
    'Z': ('#####', '   # ', '  #  ', ' #   ', '#####'),
    '0': (' ### ', '#  ##', '# # #', '##  #', ' ### '),
    '1': ('  #  ', ' ##  ', '  #  ', '  #  ', ' ### '),
    '2': (' ### ', '#   #', '  ## ', ' #   ', '#####'),
    '3': ('#### ', '    #', ' ### ', '    #', '#### '),
    '4': ('#   #', '#   #', '#####', '    #', '    #'),
    '5': ('#####', '#    ', '#### ', '    #', '#### '),
    '6': (' ### ', '#    ', '#### ', '#   #', ' ### '),
    '7': ('#####', '    #', '   # ', '  #  ', '  #  '),
    '8': (' ### ', '#   #', ' ### ', '#   #', ' ### '),
    '9': (' ### ', '#   #', ' ####', '    #', ' ### '),
    '/': ('    #', '   # ', '  #  ', ' #   ', '#    '),
    '-': ('     ', '     ', '#####', '     ', '     '),
    '.': ('     ', '     ', '     ', '     ', '  #  '),
    '!': ('  #  ', '  #  ', '  #  ', '     ', '  #  '),
    '?': (' ### ', '#   #', '  ## ', '     ', '  #  '),
    ':': ('     ', '  #  ', '     ', '  #  ', '     '),
    '_': ('     ', '     ', '     ', '     ', '#####'),
    '&': (' ##  ', '#  # ', ' ## #', '#  # ', ' ## #'),
    "'": ('  #  ', '  #  ', '     ', '     ', '     '),
    ' ': ('     ', '     ', '     ', '     ', '     '),
}


def block_letters(text, width):
    """
    Return rows of ``text`` in letters of 5 rows, or ``[text]``.

    Each letter is drawn by itself, as of the logon screens of VTAM.  The
    text is returned as it is when it does not fit ``width``.
    """
    text = text.upper()
    if len(text) * 6 - 1 > width:
        return [text]
    rows = []
    for row in range(5):
        rows.append(u' '.join(
            BLOCK_LETTERS.get(char, BLOCK_LETTERS[u' '])[row].replace(
                u'#', char if char in BLOCK_LETTERS else u' ')
            for char in text).rstrip())
    return rows


class VirtualScreen(pyte.Screen):

    """ The virtual ANSI terminal of a session. """

    def __init__(self, columns, lines, reply):
        """
        Class initializer.

        :param callable reply: receives the answers of the terminal to
            queries of the bbs, such as of the cursor position.
        """
        self._reply = reply
        #: whether the bell rang, of BEL, but not of BEL ending a sequence,
        #: such as of the title.
        self.rang = False
        pyte.Screen.__init__(self, columns, lines)

    def write_process_input(self, data):
        """ Answer a query of the bbs. """
        self._reply(data)

    def bell(self, *args):
        """ Ring the bell. """
        self.rang = True


class TN3270Client(BaseClient):

    """ A 3270 terminal. """

    kind = 'tn3270'

    BLOCKSIZE_RECV = 1024

    #: seconds without output from the bbs before its screen is drawn.
    RENDER_IDLE = 0.03

    #: but at most this many seconds after the screen first changed.
    RENDER_MAX = 0.25

    #: after an attention key, the keyboard remains locked until the bbs
    #: has answered: until this many seconds pass without its output,
    ANSWER_IDLE = 0.2

    #: or at most this many seconds.
    ANSWER_MAX = 2.0

    #: text sent without a return key is followed by one when the bbs
    #: displays it, though later than its answer, within this many seconds.
    ECHO_MAX = 10.0

    def __init__(self, sock, address_pair, on_naws=None):
        super(TN3270Client, self).__init__(sock, address_pair, on_naws)
        #: terminal type, once negotiated.
        self.ttype = None
        #: options that the terminal has agreed to, and that it refused.
        self.remote, self.local, self.refused = set(), set(), set()
        self._requested = set()
        self._state, self._command = 'data', None
        self._record, self._sb = bytearray(), bytearray()
        #: answer of the terminal to a read partition query, and the event
        #: set by it.
        self.query_reply = None
        self.query_event = threading.Event()
        self.screen = None
        self.stream = None
        self.codepage = 'bracket'
        self.input_encoding = 'utf8'
        self.rows, self.columns = 24, 80
        self.features = ('bracket', False, False, False)
        self.sent = []
        self.hidden_input = False
        self.label = PROMPT
        self.cursor = None
        self.showing_help = False
        self.help_pending = self.repaint_pending = False
        self.restore_pending = self.clear_input = False
        self.place_cursor = False
        self.alarm = False
        self.output_time = self.dirty_time = None
        #: first and last rows of input fields, declared by the bbs.
        self.fields = None
        #: name of this terminal, as of a logical unit of VTAM.
        self.terminal_id = u'X84T{0:04d}'.format(next(TERMINAL_NUMBERS))
        #: time of an attention key, until the bbs has answered it.
        self.hold_time = None
        #: (row, column, text, time) of text sent without a return key,
        #: which follows only when the bbs displays it, as a prompt of a line
        #: does.
        self.pending_return = None
        self._partial = u''

    def recv_ready(self):
        """ Whether data is awaiting on the socket. """
        return (self.is_active() and bool(
            select.select([self.sock.fileno()], [], [], 0)[0]))

    def socket_recv(self):
        """
        Receive data from socket, returns number of bytes received.

        Telnet commands are answered, and the records of the 3270 between
        them are handled by :meth:`handle_record`.

        :raises Disconnected: client has disconnected.
        :rtype: int
        """
        try:
            data = self._recv()
        except (BlockingIOError, ssl.SSLWantReadError,
                ssl.SSLWantWriteError):
            return 0
        except OSError as err:
            raise Disconnected('socket_recv error: {0}'.format(err))
        if not data:
            raise Disconnected('Closed by client (EOF)')
        self.bytes_received += len(data)
        self.last_input_time = time.time()
        for byte in data:
            self._telnet_byte(byte)
        return len(data)

    def _recv(self):
        """ Return data received from the socket. """
        return self.sock.recv(self.BLOCKSIZE_RECV)

    def _telnet_byte(self, byte):
        """ Receive one byte of the telnet stream. """
        state = self._state
        if state == 'data':
            if byte == IAC:
                self._state = 'iac'
            elif len(self._record) < RECORD_MAXLEN:
                self._record.append(byte)
        elif state == 'iac':
            self._state = 'data'
            if byte == IAC:
                if len(self._record) < RECORD_MAXLEN:
                    self._record.append(IAC)
            elif byte in (DO, DONT, WILL, WONT):
                self._state, self._command = 'option', byte
            elif byte == SB:
                self._state, self._sb = 'sb', bytearray()
            elif byte == EOR_MARK:
                record, self._record = bytes(self._record), bytearray()
                self.handle_record(record)
        elif state == 'option':
            self._state = 'data'
            self._handle_option(self._command, byte)
        elif state == 'sb':
            if byte == IAC:
                self._state = 'sb-iac'
            elif len(self._sb) < SB_MAXLEN:
                self._sb.append(byte)
        elif state == 'sb-iac':
            if byte == IAC:
                self._state = 'sb'
                self._sb.append(IAC)
            else:
                self._state = 'data'
                if byte == SE:
                    self._handle_sb(bytes(self._sb))

    def request(self, command, option):
        """ Send telnet ``command`` of ``option``, as a request. """
        self._requested.add((command, option))
        self.send_str(bytes((IAC, command, option)))

    def _handle_option(self, command, option):
        """ Answer telnet ``command`` of ``option``. """
        accepted = (TTYPE, EOR, BINARY)
        if command == WILL:
            if option not in self.remote:
                if (DO, option) not in self._requested:
                    if option not in accepted:
                        self.send_str(bytes((IAC, DONT, option)))
                        return
                    self.send_str(bytes((IAC, DO, option)))
                self.remote.add(option)
            if option == TTYPE:
                self.send_str(bytes((IAC, SB, TTYPE, TTYPE_SEND, IAC, SE)))
        elif command == DO:
            if option not in self.local:
                if (WILL, option) not in self._requested:
                    if option not in accepted or option == TTYPE:
                        self.send_str(bytes((IAC, WONT, option)))
                        return
                    self.send_str(bytes((IAC, WILL, option)))
                self.local.add(option)
        elif command == WONT:
            self.refused.add(option)
            if option in self.remote:
                self.remote.discard(option)
                self.send_str(bytes((IAC, DONT, option)))
        elif command == DONT:
            self.refused.add(option)
            if option in self.local:
                self.local.discard(option)
                self.send_str(bytes((IAC, WONT, option)))

    def _handle_sb(self, data):
        """ Receive telnet subnegotiation ``data``. """
        if len(data) > 1 and data[0] == TTYPE and data[1] == TTYPE_IS:
            self.ttype = data[2:].decode('ascii', 'replace')

    def send_record(self, record):
        """ Buffer 3270 ``record``, ending it by IAC EOR. """
        self.send_str(record.replace(b'\xff', b'\xff\xff') +
                      bytes((IAC, EOR_MARK)))

    def setup(self, rows, columns, codepage, colors, highlight, background):
        """
        Begin the virtual terminal of the bbs, and its display.

        :param int rows: rows of the 3270 screen.
        :param int columns: columns of the 3270 screen.
        :param str codepage: EBCDIC code page of the terminal.
        :param int colors: number of colors the terminal displays.
        :param bool highlight: whether it displays extended highlighting.
        :param bool background: whether it displays background colors.
        """
        self.rows, self.columns = rows, columns
        self.codepage = codepage
        self.features = (codepage, colors, highlight,
                         bool(background and colors))
        self.screen = VirtualScreen(columns, rows - 1, self.queue_keys)
        self.stream = pyte.Stream(self.screen)
        self.sent = [None] * (rows - 1)

    @property
    def prompt_addr(self):
        """ Buffer position of the prompt, ``Command ===>``, of the input row. """
        return (self.rows - 1) * self.columns

    @property
    def input_addr(self):
        """ Buffer position of the first character of the input field. """
        return self.prompt_addr + PROMPT_WIDTH + 2

    @property
    def display_addr(self):
        """ Buffer position of the attribute of the bbs display. """
        return self.rows * self.columns - 1

    @property
    def extended(self):
        """ Whether the terminal is sent extended attributes. """
        return bool(self.features[1] or self.features[2])

    @property
    def field_rows(self):
        """ Rows of input fields declared by the bbs, or an empty range. """
        if self.fields is None:
            return range(0)
        return range(self.fields[0], self.fields[1] + 1)

    def row_end(self, row):
        """
        Buffer position following the last cell of ``row`` that is drawn.

        The field of each row of input fields begins by its attribute, at
        the last cell of the row above it; the last is followed by another,
        at its own last cell.
        """
        end = (row + 1) * self.columns
        if self.fields and self.fields[0] - 1 <= row <= self.fields[1]:
            end -= 1
        return end

    def field(self, attr, color=COLOR_DEFAULT, highlight=HL_DEFAULT):
        """ Return order starting a field of attribute ``attr``. """
        if not self.extended:
            return bytes((ORDER_SF, encode_attr(attr)))
        pairs = [(XA_FIELD, encode_attr(attr))]
        if self.features[1] and color:
            pairs.append((XA_COLOR, color))
        if self.features[2] and highlight:
            pairs.append((XA_HIGHLIGHT, highlight))
        return bytes((ORDER_SFE, len(pairs))) + bytes(
            val for pair in pairs for val in pair)

    def input_field(self):
        """
        Return orders starting the input field, erasing it.

        It is erased by nulls, written in it: some terminals, as DX3270,
        display a character typed by the attribute of where it is typed,
        as of the field last begun where that was written, rather than of
        the field it is of, which may be non-display, of a password.
        """
        attr = FA_NONDISPLAY if self.hidden_input else FA_INTENSIFIED
        return (bytes((ORDER_SBA,)) + encode_address(self.input_addr - 1) +
                self.field(attr, COLORS['green'], HL_UNDERSCORE) +
                bytes((ORDER_RA,)) + encode_address(self.display_addr) +
                b'\x00')

    def label_text(self):
        """ Return orders writing the label of the input row. """
        return bytes((ORDER_SBA,)) + encode_address(
            self.prompt_addr + 1) + bytes(
                to_ebcdic(char, self.codepage)
                for char in self.label.ljust(PROMPT_WIDTH))

    def prompt_label(self, password):
        """
        Return the label of the input row, as of the bbs's prompt.

        ``Login ===>`` and ``Password ===>`` at the prompts of a login and
        of a password (``password``), and ``Command ===>`` otherwise.
        """
        if password:
            return PROMPT_PASSWORD
        if RE_LOGIN.search(self.prompt_text()):
            return PROMPT_LOGIN
        return PROMPT

    def cursor_addr(self):
        """
        Return buffer position for the cursor of the 3270.

        That of the bbs, when in an input field it declared, otherwise the
        input row.
        """
        cursor = self.screen.cursor
        if cursor.y in self.field_rows:
            return cursor.y * self.columns + min(cursor.x, self.columns - 2)
        return self.input_addr

    def send_unicode(self, ucs, encoding='utf8'):
        """ Write output of the bbs to its virtual terminal. """
        self.input_encoding = encoding
        if self.screen is None:
            return
        ucs = self._partial + ucs
        self._partial = u''
        begin = ucs.rfind(DECLARE)
        if begin >= 0 and not RE_DECLARE.match(ucs, begin) and (
                len(ucs) - begin < 64):
            # the rest of a declaration follows, with more output.
            ucs, self._partial = ucs[:begin], ucs[begin:]
        pos = 0
        for match in RE_CONTROL.finditer(ucs):
            self.feed(ucs[pos:match.start()])
            if match.group(1) is not None:
                self.declare(match.group(1))
            else:
                self.report_mode(match.group(2))
            pos = match.end()
        self.feed(ucs[pos:])
        if self.screen.rang:
            self.alarm, self.screen.rang = True, False
        now = time.time()
        self.output_time = now
        if self.dirty_time is None:
            self.dirty_time = now

    def feed(self, ucs):
        """ Write ``ucs`` to the virtual terminal. """
        if not ucs:
            return
        try:
            self.stream.feed(ucs)
        except Exception as err:
            # pyte raises on some malformed sequences, ending its parser.
            self.log.debug('{self.addrport}: virtual terminal: {err!r}'
                           .format(self=self, err=err))
            self.stream = pyte.Stream(self.screen)

    def declare(self, params):
        """
        Receive a declaration of input fields by the bbs.

        ``params`` are ``'fields;top;bottom'``, the first and last rows of
        input fields, or ``'fields'``, of none.
        """
        args = params.split(u';')
        fields = None
        if args[0] == u'fields' and len(args) == 3:
            try:
                top, bottom = int(args[1]), int(args[2])
            except ValueError:
                return
            if not 1 <= top <= bottom < self.rows - 1:
                return
            fields = (top, bottom)
        elif args != [u'fields']:
            return
        if fields != self.fields:
            self.fields = fields
            self.repaint_pending = True

    def report_mode(self, mode):
        """ Answer a query of the bbs, DECRQM, of a mode of the terminal. """
        mode = int(mode)
        state = 0
        if mode in MOUSE_MODES:
            state = 1 if (mode << 5) in self.screen.mode else 2
        self.queue_keys(u'\x1b[?{0};{1}$y'.format(mode, state))

    def mouse_click(self, addr):
        """
        Return keys of a click of the left mouse button, at ``addr``.

        Only its press: the bbs may act on it, and no longer report clicks,
        before it would receive a release, which it would then take as
        other keys.  Returns None, unless the bbs has asked for clicks to
        be reported.
        """
        mode = self.screen.mode
        if (MOUSE_MODES[0] << 5) not in mode:
            return None
        row, col = divmod(addr, self.columns)
        if (MOUSE_MODES[1] << 5) in mode:
            return u'\x1b[<0;{0};{1}M'.format(col + 1, row + 1)
        return u'\x1b[M {0}{1}'.format(chr(33 + col), chr(33 + row))

    def queue_keys(self, ucs):
        """ Buffer keystrokes ``ucs`` for the bbs. """
        with self._recv_lock:
            self.recv_buffer.extend(ucs.encode(self.input_encoding,
                                               'replace'))

    def handle_record(self, record):
        """ Handle an inbound 3270 ``record``. """
        if not record:
            return
        aid = record[0]
        if aid == AID_STRUCTURED_FIELD:
            self.query_reply = record
            self.query_event.set()
            return
        if self.screen is None:
            return
        self.restore_pending = True
        # a return key no longer follows text displayed late, of another key.
        self.pending_return = None
        if is_help(aid) and not (self.fields and aid in AID_PF and
                                 not self.showing_help):
            # in input fields, PF1 is the help of the bbs.
            self.help_pending = 'help'
            return
        if self.showing_help or aid in (AID_CLEAR, AID_PA2):
            # CLEAR has erased the screen, at the terminal.
            self.showing_help = False
            self.repaint_pending = True
            return
        aid, cursor, fields = parse_input(record)
        text = from_ebcdic(fields.get(self.input_addr, b''), self.codepage)
        self.clear_input = True
        self.hold_time = time.time()
        in_display = cursor is not None and cursor < self.prompt_addr
        if self.fields:
            name = aid_name(aid)
            if name is None:
                return
            rows = {}
            for addr, data in fields.items():
                row, col = divmod(addr, self.columns)
                if col == 0 and row in self.field_rows:
                    rows[row] = from_ebcdic(data, self.codepage)
            self.events.append(('tn3270', {
                'aid': name, 'text': text, 'fields': rows,
                'cursor': divmod(cursor, self.columns) if in_display
                else None}))
            return
        if aid == AID_ENTER and not text and in_display:
            keys = self.mouse_click(cursor)
            if keys:
                self.queue_keys(keys)
                return
        if aid == AID_ENTER and text and not self.hidden_input:
            # whether a return key follows is decided by the bbs's answer.
            cursor = self.screen.cursor
            self.pending_return = (cursor.y, cursor.x, text, time.time())
            self.queue_keys(text)
            return
        keys = keys_of(aid, text)
        if keys is not None:
            self.queue_keys(keys)

    def answered(self):
        """ Whether the bbs has answered the last attention key. """
        now = time.time()
        return (now - self.hold_time >= self.ANSWER_MAX or
                now - max(self.hold_time, self.output_time or 0)
                >= self.ANSWER_IDLE)

    def echoed(self):
        """
        Whether the bbs has displayed text sent without a return key.

        It is displayed at its cursor, as a prompt of a line does, rather
        than taken as a key of a command.
        """
        row, col, text, _ = self.pending_return
        cursor = self.screen.cursor
        line = self.screen.buffer[row]
        shown = u''.join(line[idx].data for idx in range(
            col, min(col + len(text), self.columns)))
        return ((cursor.y, cursor.x) == (row, col + len(text)) and
                shown.lower() == text.lower())

    def follow_echo(self):
        """
        Send a return key following text the bbs has displayed, or return
        False.  It is forgotten when the bbs has not, within
        :attr:`ECHO_MAX` seconds.
        """
        if self.echoed():
            self.pending_return = None
            self.queue_keys(u'\r')
            return True
        if time.time() - self.pending_return[3] >= self.ECHO_MAX:
            self.pending_return = None
        return False

    def release(self):
        """
        Unlock the keyboard, once the bbs has answered an attention key.

        Text sent without a return key is followed by one when the bbs
        displayed it, see :meth:`echoed`.  A slow bbs may display it later
        than its answer, see :meth:`draw`.
        """
        if self.pending_return and self.follow_echo():
            self.hold_time = time.time()
            return
        self.hold_time = None
        self.restore_pending = self.place_cursor = True
        for row in self.field_rows:
            # what is displayed in the fields may differ from what was
            # typed there, though the bbs did not change it.
            self.sent[row] = None
            self.screen.dirty.add(row)

    def send_ready(self):
        """ Whether there is anything to send. """
        if len(self.send_buffer):
            return True
        if self.screen is None or self.showing_help:
            return bool(self.help_pending or self.repaint_pending)
        if self.hold_time is not None and self.answered():
            return True
        if (self.help_pending or self.repaint_pending or
                self.clear_input or self.alarm or
                (self.restore_pending and self.hold_time is None)):
            return True
        if self.dirty_time is None:
            return False
        now = time.time()
        return (now - self.output_time >= self.RENDER_IDLE or
                now - self.dirty_time >= self.RENDER_MAX)

    def send(self):
        """
        Send any data buffered and return number of bytes send.

        The screen of the bbs is drawn first, when it has changed.

        :raises Disconnected: client has disconnected (cannot write to socket).
        """
        if self.screen is not None:
            try:
                self.draw()
            except Exception as err:
                # this is the engine's main loop: end only this session.
                self.log.exception('{self.addrport}: draw: {err}'
                                   .format(self=self, err=err))
                raise Disconnected('draw: {0}'.format(err))
        if not len(self.send_buffer):
            return 0
        return super(TN3270Client, self).send()

    def draw(self):
        """ Buffer a write of what has changed on the screen, if any. """
        if self.hold_time is not None and self.answered():
            self.release()
        elif self.pending_return and self.hold_time is None:
            # before the text is drawn, so that the return key is sent
            # before the terminal is seen to wait for one.
            self.follow_echo()
        if self.help_pending:
            page = self.help_pending
            self.help_pending, self.showing_help = False, True
            self.send_record(self.logon_record() if page == 'logon'
                             else self.help_record())
        elif self.repaint_pending:
            self.send_record(self.repaint_record())
        elif not self.showing_help:
            record = self.update_record()
            if record:
                self.send_record(record)

    def screen_rows(self, rows):
        """ Return dict of cells of the virtual terminal's ``rows``. """
        screen, features = self.screen, self.features
        cursor = None
        if not screen.cursor.hidden and screen.cursor.y not in self.field_rows:
            # the cursor of the 3270 is that of the bbs, in input fields.
            cursor = (screen.cursor.y, screen.cursor.x)
        result = {}
        for row in rows:
            line = screen.buffer[row]
            cells = []
            for col in range(self.row_end(row) - row * self.columns):
                char = line[col]
                cells.append(style_cell(
                    char.data, char.fg, char.bg, char.bold, char.reverse,
                    char.underscore, char.blink, (row, col) == cursor,
                    features))
            result[row] = cells
        return result

    def edit_field(self):
        """ Return order starting the input field of a row declared one. """
        return self.field(0, COLORS['white'])

    def encode_row(self, row, cells):
        """
        Return orders and characters displaying ``cells`` of ``row``.

        The field of the row is begun again, first: some terminals, as
        DX3270, give each character the attributes of the field last begun
        where it was written, rather than of the field it is of.
        """
        if row in self.field_rows:
            prefix = bytes((ORDER_SBA,)) + encode_address(
                row * self.columns - 1) + self.edit_field()
        else:
            prefix = bytes((ORDER_SBA,)) + encode_address(
                self.display_addr) + self.field(FA_SKIP)
        return prefix + encode_cells(
            cells, row * self.columns, self.row_end(row),
                            fill=0x00 if row in self.field_rows else 0x40,
                            kinds=tuple(bool(feature)
                                        for feature in self.features[1:]))

    def prompt_text(self):
        """ Return text left of the bbs's cursor, of its row. """
        screen = self.screen
        line = screen.buffer[screen.cursor.y]
        return u''.join(line[col].data for col in range(
            min(screen.cursor.x, self.columns)))

    def is_password_prompt(self):
        """ Whether the bbs's cursor follows a prompt of a password. """
        return u'pass' in self.prompt_text().lower()

    def _begin_write(self, command, wcc):
        """ Return the beginning of a write command, and reset flags. """
        if self.alarm:
            wcc |= WCC_ALARM
        if self.hold_time is None:
            wcc |= WCC_RESTORE
            self.restore_pending = False
        self.alarm = False
        self.dirty_time = None
        return bytearray((command, encode_attr(wcc)))

    def repaint_record(self):
        """ Return a write of the whole screen. """
        self.repaint_pending = self.clear_input = self.place_cursor = False
        self.hidden_input = self.is_password_prompt()
        self.label = self.prompt_label(self.hidden_input)
        self.cursor = (None if self.screen.cursor.hidden
                       else self.screen.cursor.y)
        command = (CMD_EW if (self.rows, self.columns) == (24, 80)
                   else CMD_EWA)
        out = self._begin_write(command, WCC_RESET_MDT)
        rows = self.screen_rows(range(self.rows - 1))
        for row, cells in rows.items():
            out += self.encode_row(row, cells)
            self.sent[row] = cells
        self.screen.dirty.clear()
        out += bytes((ORDER_SBA,)) + encode_address(self.prompt_addr)
        out += self.field(FA_SKIP | FA_INTENSIFIED, COLORS['cyan'])
        out += self.label_text()
        out += self.input_field()
        out += bytes((ORDER_SBA,)) + encode_address(self.display_addr)
        out += self.field(FA_SKIP)
        for row in self.field_rows:
            out += bytes((ORDER_SBA,)) + encode_address(
                row * self.columns - 1)
            out += self.edit_field()
        if self.fields:
            out += bytes((ORDER_SBA,)) + encode_address(
                self.row_end(self.fields[1]))
            out += self.field(FA_SKIP)
        out += bytes((ORDER_SBA,)) + encode_address(self.cursor_addr())
        out += bytes((ORDER_IC,))
        return bytes(out)

    def update_record(self):
        """ Return a write of what has changed on the screen, or None. """
        screen = self.screen
        cursor = (None if screen.cursor.hidden else screen.cursor.y)
        changed = set(screen.dirty) | {
            row for row in (self.cursor, cursor) if row is not None}
        screen.dirty.clear()
        self.cursor = cursor
        hidden = self.is_password_prompt()
        label = self.prompt_label(hidden)
        rows = {row: cells for row, cells in self.screen_rows(
            row for row in sorted(changed) if row < self.rows - 1).items()
            if cells != self.sent[row]}
        restore = self.restore_pending and self.hold_time is None
        if not (rows or restore or self.clear_input or self.place_cursor or
                self.alarm or hidden != self.hidden_input or
                label != self.label):
            self.dirty_time = None
            return None
        out = self._begin_write(
            CMD_W, WCC_RESET_MDT if self.clear_input else 0)
        if self.clear_input:
            self.clear_input = False
            out += bytes((ORDER_SBA,)) + encode_address(self.input_addr)
            out += bytes((ORDER_EUA,)) + encode_address(self.display_addr)
        if label != self.label:
            self.label = label
            out += self.label_text()
        if hidden != self.hidden_input:
            self.hidden_input = hidden
            out += self.input_field()
        for row, cells in rows.items():
            out += self.encode_row(row, cells)
            self.sent[row] = cells
        if self.place_cursor:
            self.place_cursor = False
            out += bytes((ORDER_SBA,)) + encode_address(self.cursor_addr())
            out += bytes((ORDER_IC,))
        return bytes(out)

    def page_record(self, items):
        """
        Return a write of a page of text, such as of the keys.

        :param list items: ``(row, column, color, text)`` of each line, of
            a protected field, its attribute left of ``column``.
        """
        command = (CMD_EW if (self.rows, self.columns) == (24, 80)
                   else CMD_EWA)
        out = self._begin_write(command, WCC_RESTORE | WCC_RESET_MDT)
        for row, col, color, text in items:
            if not 0 <= row < self.rows or not text:
                continue
            col = max(1, col)
            out += bytes((ORDER_SBA,)) + encode_address(
                row * self.columns + col - 1)
            out += self.field(FA_SKIP | (FA_INTENSIFIED if color in (
                COLORS['white'], COLORS['yellow']) else 0), color)
            out += bytes(to_ebcdic(char, self.codepage)
                         for char in text[:self.columns - col - 1])
        out += bytes((ORDER_SBA,)) + encode_address(0) + bytes((ORDER_IC,))
        return bytes(out)

    def key_lines(self):
        """ Return lines naming the keys, as ISPF does. """
        return [
            u'PF1  = HELP        PF2  = BACKSPACE   PF3  = END',
            u'PF4  = NO RETURN   PF5  = UP          PF6  = DOWN',
            u'PF7  = PAGE UP     PF8  = PAGE DOWN   PF9  = HOME',
            u'PF10 = LEFT        PF11 = RIGHT       PF12 = CANCEL',
            u'PF13-PF24 ARE PF1-PF12, BUT PF16 = TAB, PF21 = END OF LINE',
            u'PA1  = HELP        PA2  = REDRAW      CLEAR = REDRAW',
        ]

    def help_record(self):
        """ Return a write of the page describing the keys. """
        from x84.bbs import get_ini
        bbsname = (get_ini('system', 'bbsname') or 'x/84').upper()
        lines = ([(COLORS['yellow'], u'{0} - TN3270 KEYS'.format(bbsname)),
                  (None, u'')] +
                 [(COLORS['cyan'], line) for line in (
                     u'TYPE AFTER COMMAND ===> AND PRESS ENTER.  OR MOVE THE '
                     u'CURSOR TO',
                     u'A CHOICE OF A MENU OR LIST, OR A KEY NAMED AT THE '
                     u'BOTTOM OF A',
                     u'SCREEN, AND PRESS ENTER.  TYPE IN THE WINDOW OF THE '
                     u'EDITOR.')] +
                 [(None, u'')] +
                 [(COLORS['green'], line) for line in self.key_lines()] +
                 [(None, u''), (COLORS['white'], u'PRESS ENTER TO RETURN.')])
        top = max(0, (self.rows - len(lines)) // 3)
        return self.page_record([(top + idx, 2, color, text)
                                 for idx, (color, text) in enumerate(lines)])

    def logon_record(self):
        """
        Return a write of the logon screen, as of VTAM: the name of the
        bbs in large letters, and of this terminal and the keys.
        """
        from x84.bbs import get_ini
        bbsname = (get_ini('system', 'bbsname') or 'x/84').upper()
        now = time.localtime()
        logo = block_letters(bbsname, self.columns - 4)
        top = 1 if self.rows < 30 else 3
        # centered as a whole, its rows being of differing lengths.
        logo_left = (self.columns - max(len(line) for line in logo)) // 2
        items = [(top + idx, logo_left, COLORS['cyan'], line)
                 for idx, line in enumerate(logo)]
        row = top + len(logo) + 1
        left = max(2, (self.columns - 60) // 2)
        for label, value in (
                (u'SYSTEM', bbsname),
                (u'TERMINAL', self.terminal_id),
                (u'MODEL', u'{0}  {1} X {2}'.format(
                    (self.ttype or u'').upper(), self.rows, self.columns)),
                (u'DATE', time.strftime(u'%m/%d/%y', now)),
                (u'TIME', time.strftime(u'%H:%M:%S', now))):
            # a leader of dots, at the odd columns, as ISPF's.
            leader = u''.join(u'.' if col % 2 and col > len(label) else u' '
                              for col in range(len(label), 15))
            items.append((row, left, COLORS['green'], label + leader + u':'))
            items.append((row, left + 17, COLORS['white'], value))
            row += 1
        row += 1
        for line in self.key_lines()[:4]:
            items.append((row, left, COLORS['blue'], line))
            row += 1
        items.append((row + 1, left, COLORS['yellow'],
                      u'PRESS ENTER TO LOG ON TO {0}'.format(bbsname)))
        return self.page_record(items)


class TN3270TLSClient(TN3270Client):

    """
    A 3270 terminal connected by TLS.

    The engine does not read the socket until :class:`ConnectTN3270TLS`
    has completed the TLS handshake.  A TLS connection must not be used by
    two threads at once, and the negotiating thread sends while the engine
    receives, so the socket is used only under a lock.
    """

    kind = 'tn3270-tls'

    def __init__(self, sock, address_pair, on_naws=None, context=None):
        sock = context.wrap_socket(sock, server_side=True,
                                   do_handshake_on_connect=False)
        super(TN3270TLSClient, self).__init__(sock, address_pair, on_naws)
        #: whether the TLS handshake has completed.
        self.handshaken = False
        self._sock_lock = threading.RLock()

    def fileno(self):
        """ File descriptor number of socket, None until the handshake. """
        if not self.handshaken:
            return None
        return super(TN3270TLSClient, self).fileno()

    def recv_ready(self):
        """ Whether data is awaiting on the socket. """
        return self.handshaken and super(TN3270TLSClient, self).recv_ready()

    def _recv(self):
        """
        Return data received from the socket.

        Data decrypted beyond the size asked for waits inside the TLS
        connection, where select does not see it, so it is received too.
        """
        with self._sock_lock:
            data = self.sock.recv(self.BLOCKSIZE_RECV)
            while data and self.sock.pending():
                data += self.sock.recv(self.sock.pending())
        return data

    def send(self):
        """
        Send any data buffered and return number of bytes send.

        :raises Disconnected: client has disconnected (cannot write to socket).
        """
        with self._sock_lock:
            return super(TN3270TLSClient, self).send()

    def shutdown(self):
        """ End the TLS connection, then shutdown and close socket. """
        with self._sock_lock:
            if self.handshaken:
                # tell the terminal that the connection ends, as some report
                # an error when it closes without this.
                try:
                    self.sock.unwrap()
                except (OSError, ValueError):
                    pass
            super(TN3270TLSClient, self).shutdown()


class ConnectTN3270(BaseConnect):

    """ Negotiate with a 3270 terminal, then spawn its session. """

    #: maximum time for each stage of negotiation.
    TIME_NEGOTIATE = 5.0

    #: maximum time for the terminal to answer a query.
    TIME_QUERY = 2.0

    #: poll interval during negotiation.
    TIME_POLL = 0.05

    def run(self):
        """ Negotiate terminal type and 3270 mode, then spawn session. """
        try:
            self._set_socket_opts()
            self.negotiate()
            if self.client.is_active():
                return spawn_client_session(client=self.client)
        except ValueError as err:
            self.log.info('{client.addrport}: {err}'
                          .format(client=self.client, err=err))
            self.not_3270()
        except (Disconnected, OSError) as err:
            self.log.debug('{client.addrport}: connection closed: {err}'
                           .format(client=self.client, err=err))
        except Exception as err:
            self.log.exception('{client.addrport}: connection closed: {err}'
                               .format(client=self.client, err=err))
        finally:
            self.stopped = True
        self.client.deactivate()

    def flush(self):
        """ Send what is buffered for the terminal. """
        if self.client.send_ready():
            self.client.send()

    def wait_for(self, done, timeout):
        """ Wait until ``done()`` or ``timeout``, returning ``done()``. """
        st_time = time.time()
        while not done() and time.time() - st_time < timeout:
            if not self.client.is_active():
                raise Disconnected('closed during negotiation')
            self.flush()
            time.sleep(self.TIME_POLL)
        self.flush()
        return done()

    def negotiate(self):
        """
        Negotiate terminal type, binary and end of record options.

        :raises ValueError: the client is not a 3270 terminal.
        """
        from x84.bbs import get_ini
        client = self.client
        client.request(DO, TTYPE)
        if not self.wait_for(lambda: client.ttype is not None or
                             TTYPE in client.refused, self.TIME_NEGOTIATE):
            raise ValueError('no terminal type')
        if not (client.ttype or u'').upper().startswith('IBM-'):
            raise ValueError('terminal type {0!r} is not a 3270'
                             .format(client.ttype))
        for command, option in ((DO, EOR), (WILL, EOR),
                                (DO, BINARY), (WILL, BINARY)):
            client.request(command, option)
        if not self.wait_for(
                lambda: (client.remote >= {EOR, BINARY} and
                         client.local >= {EOR, BINARY}) or
                client.refused & {EOR, BINARY}, self.TIME_NEGOTIATE):
            raise ValueError('no binary and end of record mode')
        if client.refused & {EOR, BINARY}:
            raise ValueError('binary or end of record mode refused')

        rows, columns, extended = parse_ttype(client.ttype)
        codes, colors, reply = set(), 0, None
        if extended:
            client.send_record(QUERY)
            if self.wait_for(client.query_event.is_set, self.TIME_QUERY):
                reply = parse_query_reply(client.query_reply)
                codes, colors = reply['codes'], reply['colors']
                if reply['size'] and 12 <= reply['size'][0] <= 255 and (
                        64 <= reply['size'][1] <= 255):
                    rows, columns = reply['size']
            else:
                self.log.debug('{client.addrport}: no answer to query'
                               .format(client=client))
        codepage = choose_codepage(get_ini('tn3270', 'codepage'), reply)
        # background colors are displayed by terminals of 16 colors, as the
        # x3270 family, unless configured otherwise.
        background = (get_ini('tn3270', 'background') or 'auto').lower()
        background = (background in ('yes', 'true', 'on', '1') or (
            background == 'auto' and colors >= 16))
        client.setup(rows, columns, codepage, colors=colors,
                     highlight=QR_HIGHLIGHT in codes, background=background)
        self.log.info('{client.addrport}: {client.ttype}, {rows}x{columns}'
                      '{colors}, {codepage}'.format(
                          client=client, rows=rows, columns=columns,
                          colors=', {0} colors'.format(colors)
                          if colors else u'', codepage=codepage))
        env = client.env
        env['TERM'] = get_ini('tn3270', 'term') or 'xterm-256color'
        env['encoding'] = 'utf8'
        env['LINES'] = str(rows - 1)
        env['COLUMNS'] = str(columns)
        env['TN3270_TTYPE'] = client.ttype
        client.help_pending = 'logon'
        client.draw()
        self.flush()

    def not_3270(self):
        """ Tell a client that is not a 3270 terminal where to connect. """
        from x84.bbs import get_ini
        port = get_ini('telnet', 'port')
        self.client.send_str(
            b'\r\nThis port is for 3270 terminals (tn3270).' +
            (u'  Connect by telnet to port {0}.'.format(port).encode('ascii')
             if port else b'') + b'\r\n')
        try:
            self.flush()
        except Disconnected:
            pass


class ConnectTN3270TLS(ConnectTN3270):

    """ Complete the TLS handshake with a 3270 terminal, then negotiate. """

    def _set_socket_opts(self):
        """
        Complete the TLS handshake, then set socket options.

        :raises Disconnected: the handshake failed.
        """
        client = self.client
        client.sock.settimeout(self.TIME_NEGOTIATE)
        try:
            client.sock.do_handshake()
        except OSError as err:
            # such as by a terminal that refuses the certificate, or one
            # that is not connecting by TLS.
            self.log.info('{client.addrport}: tls handshake failed: {err}'
                          .format(client=client, err=err))
            raise Disconnected('tls handshake: {0}'.format(err))
        super(ConnectTN3270TLS, self)._set_socket_opts()
        client.handshaken = True
        self.log.debug('{client.addrport}: {version}, {cipher}'.format(
            client=client, version=client.sock.version(),
            cipher=client.sock.cipher()[0]))


class TN3270Server(BaseServer):

    """ Server for 3270 terminals, by the tn3270 protocol. """

    client_factory = TN3270Client
    connect_factory = ConnectTN3270
    config_section = 'tn3270'
    default_port = 3270


class TN3270TLSServer(TN3270Server):

    """ Server for 3270 terminals connecting by TLS, on ``tls_port``. """

    client_factory = TN3270TLSClient
    connect_factory = ConnectTN3270TLS
    port_option = 'tls_port'
    name = 'tn3270 tls'

    def __init__(self, config):
        """
        Class initializer: load the certificate, then bind and listen.

        :raises SystemExit: the certificate or key could not be loaded, or
            the address could not be bound.
        """
        log = logging.getLogger(__name__)
        cert, key = (os.path.expanduser(config.get(
            self.config_section, option, fallback=u''))
            for option in ('cert', 'key'))
        if not (cert and key):
            log.error('[tn3270] tls_port requires options cert and key')
            raise SystemExit(1)
        try:
            #: :class:`ssl.SSLContext` for its connections.
            self.context = get_ssl_context(cert, key)
        except ValueError as err:
            log.error(err)
            raise SystemExit(1)
        super(TN3270TLSServer, self).__init__(config)

    @classmethod
    def client_factory_kwargs(cls, instance):
        """ Return keyword arguments for the client_factory. """
        return {'context': instance.context}
