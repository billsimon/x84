"""
IRC chat script for x/84.

Joins one channel of an IRC network, by the ``[irc]`` section of the
configuration.  The screen is the messages of the channel, newest at the
bottom, and the input line below them, of the channel's name: type to say
something, or a command, such as ``/help``.  Page Up and Page Down (PF7
and PF8 at a 3270) scroll back through the messages.

Text received is displayed only as text: the formatting of IRC (bold,
colors, and the like) is translated to that of the terminal, and any other
control character, such as of an escape sequence, is discarded.

Options of section ``[irc]``:

- ``server``, ``port``: the server, ``irc.libera.chat`` port ``6697``.
- ``ssl``: whether to connect by TLS; ``ssl_verify``, whether to verify the
  server's certificate, ``yes`` by default.
- ``channel``: the channel joined, ``#x84``.
- ``sasl_user``, ``sasl_password``: an account to log in to by SASL,
  as networks such as Libera.Chat prefer; or ``password``, a password of
  the server, sent by PASS.
- ``max_nick``: the longest nick, ``16``.  A user's nick is of their
  handle; one in use is followed by ``_`` or a digit.
- ``connect_timeout``: seconds to wait for the server, ``30``.
- ``quit_message``: the message of quitting, the bbs's name by default.
- ``enable_privnotice``: whether to display notices of the server, such
  as while connecting.
- ``max_scrollback``: messages kept to scroll back to, ``200``;
  ``max_input``, the longest line typed, ``200``.
- ``color_inputbar``: color of the input line's prompt, ``reverse``;
  ``syncterm_font``, the font of SyncTERM, ``topaz``.
"""
# std imports
import codecs
import collections
import logging
import re
import socket
import ssl
import threading
import time
import unicodedata
import zlib

# local
from x84.bbs import getsession, getterminal, echo, get_ini, syncterm_setfont
from x84.bbs.textbuffer import char_width, text_width, wrap_offsets
from editor import ctrl, is_escape

# 3rd party
import irc.client
import irc.events
from jaraco.stream.buffer import LenientDecodingLineBuffer

#: how many messages of scrollback to keep.
MAX_SCROLLBACK = get_ini('irc', 'max_scrollback', getter='getint') or 200

#: how many characters of input to allow.
MAX_INPUT = get_ini('irc', 'max_input', getter='getint') or 200

#: irc server
SERVER = get_ini('irc', 'server') or 'irc.libera.chat'

#: irc port
PORT = get_ini('irc', 'port', getter='getint') or 6697

#: irc channel joined on connecting.
CHANNEL = get_ini('irc', 'channel') or '#x84'

#: irc server password, sent by PASS.
PASSWORD = get_ini('irc', 'password') or None

#: account and password of SASL login, when both are set.
SASL_USER = get_ini('irc', 'sasl_user') or None
SASL_PASSWORD = get_ini('irc', 'sasl_password') or None

#: maximum length of irc nicks.
MAX_NICK = (get_ini('irc', 'max_nick', getter='getint') or
            get_ini('irc', 'maxnick', getter='getint') or 16)

#: whether irc server requires ssl
ENABLE_SSL = get_ini('irc', 'ssl', getter='getboolean')

#: whether to verify the irc server's ssl certificate
SSL_VERIFY = (get_ini('irc', 'ssl_verify', getter='getboolean')
              if get_ini('irc', 'ssl_verify') else True)

#: seconds to wait for the server to answer, connecting.
CONNECT_TIMEOUT = get_ini('irc', 'connect_timeout', getter='getint') or 30

#: Whether to display notices of the server (such as of connecting).
ENABLE_PRIVNOTICE = get_ini('irc', 'enable_privnotice', getter='getboolean')

#: message of quitting irc.
QUIT_MESSAGE = (get_ini('irc', 'quit_message') or
                get_ini('system', 'bbsname') or u'x/84 bbs')

#: color of the input line.
COLOR_INPUTBAR = get_ini('irc', 'color_inputbar') or 'reverse'

#: syncterm font to use
SYNCTERM_FONT = get_ini('irc', 'syncterm_font') or 'topaz'

#: answer to CTCP VERSION.
VERSION = u'x/84 bbs ircchat'

#: terminal colors of mIRC colors 0 to 15: their bright colors, of names
#: 'bold_', are displayed by :func:`fg_sequence`.
MIRC_COLORS = (
    'bold_white', 'black', 'blue', 'green', 'bold_red', 'red',
    'magenta', 'yellow', 'bold_yellow', 'bold_green', 'cyan',
    'bold_cyan', 'bold_blue', 'bold_magenta', 'bold_black',
    'white',
)

#: rgb of mIRC colors 0 to 15, of which the nearest is displayed for others
#: at terminals of fewer than 256 colors.
MIRC_RGB = (
    0xffffff, 0x000000, 0x00007f, 0x009300, 0xff0000, 0x7f0000, 0x9c009c,
    0xfc7f00, 0xffff00, 0x00fc00, 0x009393, 0x00ffff, 0x0000fc, 0xff00ff,
    0x7f7f7f, 0xd2d2d2,
)

#: rgb of the extended mIRC colors, 16 to 98.
MIRC_EXTENDED = (
    0x470000, 0x472100, 0x474700, 0x324700, 0x004700, 0x00472c, 0x004747,
    0x002747, 0x000047, 0x2e0047, 0x470047, 0x47002a, 0x740000, 0x743a00,
    0x747400, 0x517400, 0x007400, 0x007449, 0x007474, 0x004074, 0x000074,
    0x4b0074, 0x740074, 0x740045, 0xb50000, 0xb56300, 0xb5b500, 0x7db500,
    0x00b500, 0x00b571, 0x00b5b5, 0x0063b5, 0x0000b5, 0x7500b5, 0xb500b5,
    0xb5006b, 0xff0000, 0xff8c00, 0xffff00, 0xb2ff00, 0x00ff00, 0x00ffa0,
    0x00ffff, 0x008cff, 0x0000ff, 0xa500ff, 0xff00ff, 0xff0098, 0xff5959,
    0xffb459, 0xffff71, 0xcfff60, 0x6fff6f, 0x65ffc9, 0x6dffff, 0x59b4ff,
    0x5959ff, 0xc459ff, 0xff66ff, 0xff59bc, 0xff9c9c, 0xffd39c, 0xffff9c,
    0xe2ff9c, 0x9cff9c, 0x9cffdb, 0x9cffff, 0x9cd3ff, 0x9c9cff, 0xdc9cff,
    0xff9cff, 0xff94d3, 0x000000, 0x131313, 0x282828, 0x363636, 0x4d4d4d,
    0x656565, 0x818181, 0x9f9f9f, 0xbcbcbc, 0xe2e2e2, 0xffffff,
)

#: mIRC colors of nicks, chosen by each nick.
NICK_COLORS = (3, 4, 6, 7, 9, 10, 11, 12, 13)

#: mIRC colors of pipe codes |00 to |15, which are of ANSI colors.
PIPE_MIRC = (1, 2, 3, 10, 5, 6, 7, 15, 14, 12, 9, 11, 4, 13, 8, 0)

#: pipe codes of messages typed.
RE_PIPE = re.compile(r'\|(0[0-9]|1[0-5]|b[0-7]|bb|uu|rr|nn)', re.IGNORECASE)

#: characters of nicks, but letters and digits.
NICK_SPECIAL = u'[]\\`_^{|}'

#: bytes of a message sent, of a line of at most 512 bytes as relayed to
#: others, with the prefix of its sender.
MESSAGE_BYTES = 400

#: indentation of rows that continue a message, past its time.
INDENT = 6

#: formatting characters of IRC, toggling a style.
TOGGLES = {u'\x02': 'bold', u'\x1d': 'italic', u'\x1f': 'underline',
           u'\x16': 'reverse'}

#: formatting characters of IRC not displayed: strikethrough, monospace.
IGNORED = u'\x1e\x11'

#: formatting characters of IRC: reset, color, hex color.
RESET, COLOR, HEX_COLOR = u'\x0f', u'\x03', u'\x04'

RE_COLOR = re.compile(r'([0-9]{1,2})(?:,([0-9]{1,2}))?')
RE_HEX_COLOR = re.compile(r'([0-9a-fA-F]{6})(?:,([0-9a-fA-F]{6}))?')

#: characters that change the direction of text, not displayed.
BIDI = frozenset(u'\u200e\u200f\u202a\u202b\u202c\u202d\u202e'
                 u'\u2066\u2067\u2068\u2069')

#: events of numeric replies of errors, displayed as they are.
ERROR_EVENTS = frozenset(
    name for code, name in irc.events.numeric.items()
    if 400 <= int(code) < 600)

#: events of a nick refused, of which another is tried, connecting.
NICK_REFUSED = ('nicknameinuse', 'erroneusnickname', 'nickcollision',
                'unavailresource')

#: style of text, of IRC formatting: ``fg`` and ``bg`` are mIRC colors, 0
#: to 98, or None.
Style = collections.namedtuple(
    'Style', ['bold', 'italic', 'underline', 'reverse', 'fg', 'bg'])

PLAIN = Style(False, False, False, False, None, None)


def is_displayed(ucs):
    """ Whether character ``ucs`` of text received may be displayed. """
    code = ord(ucs)
    return not (code < 0x20 or 0x7f <= code < 0xa0 or ucs in BIDI or
                ucs in u'\u2028\u2029')


def parse_formatting(text, base=PLAIN):
    """
    Return ``text`` of IRC formatting as runs of ``(style, text)``.

    Formatting begins of style ``base``, and a reset returns to it.  Other
    control characters are discarded, such as of escape sequences, which
    are then displayed as text; tabs are spaces.
    """
    return parse_styled(text, base, base)[0]


def parse_styled(text, base, style):
    """
    Return runs of ``text``, as :func:`parse_formatting`, beginning of
    ``style``, and the style it ends of.
    """
    runs, chars = [], []

    def flush():
        if chars:
            runs.append((style, u''.join(chars)))
            del chars[:]

    idx = 0
    while idx < len(text):
        ucs = text[idx]
        idx += 1
        if ucs in TOGGLES:
            flush()
            field = TOGGLES[ucs]
            style = style._replace(**{field: not getattr(style, field)})
        elif ucs == RESET:
            flush()
            style = base
        elif ucs in (COLOR, HEX_COLOR):
            flush()
            match = (RE_COLOR if ucs == COLOR else RE_HEX_COLOR).match(
                text, idx)
            if match is None:
                style = style._replace(fg=base.fg, bg=base.bg)
                continue
            idx = match.end()
            fg = parse_color(match.group(1), base.fg)
            bg = (style.bg if match.group(2) is None else
                  parse_color(match.group(2), base.bg))
            style = style._replace(fg=fg, bg=bg)
        elif ucs == u'\t':
            chars.append(u' ')
        elif ucs in IGNORED or not is_displayed(ucs):
            continue
        else:
            chars.append(ucs)
    flush()
    return runs, style


def parse_color(value, default):
    """
    Return color of formatting ``value``: a mIRC color, as a number of 2
    digits or fewer, or of 6 hex digits, an rgb integer, beyond the mIRC
    colors.  99 is the ``default``.
    """
    if len(value) == 6:
        return 0x1000000 + int(value, 16)
    number = int(value)
    return default if number == 99 else number


def color_rgb(color):
    """ Return rgb integer of ``color``, as of :func:`parse_color`. """
    if color >= 0x1000000:
        return color - 0x1000000
    if color < 16:
        return MIRC_RGB[color]
    return MIRC_EXTENDED[color - 16]


def nearest_mirc(rgb):
    """ Return the mIRC color, of 0 to 15, nearest ``rgb``. """
    def distance(other):
        return sum((((rgb >> shift) & 0xff) - ((other >> shift) & 0xff)) ** 2
                   for shift in (16, 8, 0))
    return min(range(16), key=lambda idx: distance(MIRC_RGB[idx]))


#: numbers of the terminal's colors, by name.
ANSI_COLORS = ('black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan',
               'white')


def fg_sequence(term, name):
    """
    Return sequence of the terminal displaying color ``name`` of text, of
    :data:`MIRC_COLORS`.

    A bright color is the terminal's own, of 16 colors or more; else it is
    bold, as of an ansi terminal, as of a bbs; of others, such as of
    xterm, of 8 colors by their terminfo, which may display bold of the
    normal color, it is also of its own color, 90 to 97.
    """
    if not name.startswith('bold_'):
        return getattr(term, name)
    color = ANSI_COLORS.index(name[len('bold_'):])
    if term.number_of_colors >= 16:
        return term.color(color + 8)
    sequence = term.bold + term.color(color)
    if not (term.kind or u'').startswith('ansi'):
        sequence += u'\x1b[{0}m'.format(90 + color)
    return sequence


def style_sequence(term, style):
    """ Return sequence of the terminal displaying ``style``. """
    parts = [term.normal]
    if style.bold:
        parts.append(term.bold)
    if style.italic:
        parts.append(term.italic)
    if style.underline:
        parts.append(term.underline)
    if style.reverse:
        parts.append(term.reverse)
    rgb_ok = term.number_of_colors >= 256
    if style.fg is not None:
        if style.fg < 16 or not rgb_ok:
            fg = (style.fg if style.fg < 16 else
                  nearest_mirc(color_rgb(style.fg)))
            # black, of a terminal's black background, is dark gray.
            if fg == 1 and style.bg is None:
                fg = 14
            parts.append(fg_sequence(term, MIRC_COLORS[fg]))
        else:
            parts.append(term.color_rgb(*rgb_tuple(color_rgb(style.fg))))
    if style.bg is not None:
        if style.bg < 16 or not rgb_ok:
            bg = (style.bg if style.bg < 16 else
                  nearest_mirc(color_rgb(style.bg)))
            # the bright colors of a background are its normal ones.
            parts.append(getattr(term, 'on_' + MIRC_COLORS[bg].replace(
                'bold_', '')))
        else:
            parts.append(term.on_color_rgb(*rgb_tuple(color_rgb(style.bg))))
    return u''.join(parts)


def rgb_tuple(rgb):
    """ Return (red, green, blue) of rgb integer ``rgb``. """
    return (rgb >> 16) & 0xff, (rgb >> 8) & 0xff, rgb & 0xff


def plain_text(runs):
    """ Return the text of ``runs``. """
    return u''.join(text for _, text in runs)


def fold_text(text, encoding):
    """
    Return ``text`` as displayed by a terminal of ``encoding``.

    A character not of the encoding is of its letter, as of an accent
    removed, or ``?``, as is written to the terminal, so that it may be
    measured, one cell.  Text is as it is, of utf-8.
    """
    if is_utf8(encoding):
        return text
    result = []
    for ucs in unicodedata.normalize('NFC', text):
        if unicodedata.combining(ucs):
            continue
        for candidate in (ucs, unicodedata.normalize('NFKD', ucs)[:1]):
            try:
                candidate.encode(encoding)
            except (UnicodeEncodeError, LookupError):
                continue
            if candidate:
                result.append(candidate)
                break
        else:
            result.append(u'?')
    return u''.join(result)


def is_utf8(encoding):
    """ Whether ``encoding`` is utf-8. """
    try:
        return codecs.lookup(encoding).name == 'utf-8'
    except LookupError:
        return False


def slice_runs(runs, start, end):
    """ Return the part of ``runs`` of text offsets ``start`` to ``end``. """
    result, pos = [], 0
    for style, text in runs:
        first, last = max(start, pos), min(end, pos + len(text))
        if first < last:
            result.append((style, text[first - pos:last - pos]))
        pos += len(text)
    return result


def wrap_runs(runs, width):
    """
    Return ``runs`` wrapped to ``width`` cells, as a list of rows of runs.

    Spaces that end a row are removed.
    """
    text = plain_text(runs)
    offsets = wrap_offsets(text, max(1, width)) + (len(text),)
    rows = []
    for start, end in zip(offsets, offsets[1:]):
        row = slice_runs(runs, start, end)
        while row and not row[-1][1].rstrip(u' '):
            row.pop()
        if row:
            row[-1] = (row[-1][0], row[-1][1].rstrip(u' '))
        rows.append(row)
    return rows


def render_runs(term, runs):
    """ Return sequence displaying ``runs``, ending of normal style. """
    return u''.join(style_sequence(term, style) + text
                    for style, text in runs) + term.normal


def fit_tail(text, width):
    """ Return the end of ``text`` that fits ``width`` cells. """
    cells, idx = 0, len(text)
    while idx > 0 and cells + char_width(text[idx - 1]) <= width:
        idx -= 1
        cells += char_width(text[idx])
    return text[idx:]


def sanitize_nick(handle, max_length):
    """ Return a nick of IRC, of user ``handle``. """
    text = unicodedata.normalize('NFKD', handle).encode(
        'ascii', 'ignore').decode('ascii')
    nick = u''.join(ucs if ucs.isalnum() or ucs in NICK_SPECIAL + u'-'
                    else u'_' for ucs in text)
    if not nick or not (nick[0].isalpha() or nick[0] in NICK_SPECIAL):
        nick = u'_' + nick
    return nick[:max_length]


def nick_candidates(handle, max_length):
    """
    Return nicks to try of user ``handle``, in order: of the handle, then
    of ``_`` or a digit added, and the same of at most 9 characters, the
    least that servers allow.
    """
    result = []
    for length in (max_length, 9):
        base = sanitize_nick(handle, length)
        for nick in [base, base[:length - 1] + u'_'] + [
                base[:length - 1] + str(digit) for digit in range(1, 10)]:
            if nick not in result:
                result.append(nick)
    return result


def split_message(text, limit=MESSAGE_BYTES):
    """
    Return ``text`` split to messages of at most ``limit`` bytes of utf-8.

    Messages are split at spaces, where they may be, else of characters.
    """
    messages = []
    while len(text.encode('utf8')) > limit:
        # the most characters fitting the limit, then back to a space.
        end, size = 0, 0
        while end < len(text):
            width = len(text[end].encode('utf8'))
            if size + width > limit:
                break
            size += width
            end += 1
        space = text.rfind(u' ', 0, end + 1)
        if space > 0:
            end = space
        messages.append(text[:end])
        text = text[end:].lstrip(u' ')
    if text:
        messages.append(text)
    return messages


def mirc_encode(text):
    """ Return ``text`` of pipe codes, such as ``|04``, as of mIRC codes. """
    def replace(match):
        value = match.group(1).lower()
        if value == 'bb':
            return u'\x02'
        if value == 'uu':
            return u'\x1f'
        if value == 'rr':
            return u'\x16'
        if value == 'nn':
            return u'\x0f'
        if value[0] == 'b':
            return u'\x0399,{0:02d}'.format(PIPE_MIRC[int(value[1])])
        return u'\x03{0:02d}'.format(PIPE_MIRC[int(value)])
    return RE_PIPE.sub(replace, text)


def nick_color(nick):
    """ Return the mIRC color of ``nick``, the same of each. """
    return NICK_COLORS[zlib.crc32(nick.lower().encode('utf8')) %
                       len(NICK_COLORS)]


def mentions(text, nick):
    """ Whether message ``text`` names ``nick``, as a word. """
    if not nick:
        return False
    word = u'A-Za-z0-9' + re.escape(NICK_SPECIAL + u'-')
    return re.search(u'(?<![{0}]){1}(?![{0}])'.format(word, re.escape(nick)),
                     text, re.IGNORECASE) is not None


def fmt(template, *args):
    """
    Return runs of ``template``, of IRC formatting, such as ``\\x02`` for
    bold, its ``{}`` replaced by ``args``.

    An argument of text is of the style of the template where it is; one
    of runs is as they are, such as of a nick, or of text received.
    """
    runs, style = [], PLAIN
    for idx, part in enumerate(template.split(u'{}')):
        part_runs, style = parse_styled(part, PLAIN, style)
        runs.extend(part_runs)
        if idx < len(args):
            arg = args[idx]
            if isinstance(arg, str):
                arg = [(style, u''.join(ucs for ucs in arg
                                        if is_displayed(ucs)))]
            runs.extend(arg)
    return runs


def text_runs(text, style=PLAIN):
    """ Return runs of ``text`` received, of its IRC formatting. """
    return parse_formatting(text, style)


def reason_runs(reason):
    """ Return runs of `` (reason)``, of a quit or the like, if any. """
    return fmt(u' ({})', text_runs(reason)) if reason.strip() else []


def nick_runs(nick, style=None):
    """ Return runs of ``nick``, of its color. """
    style = style or PLAIN._replace(fg=nick_color(nick))
    return [(style, u''.join(ucs for ucs in nick if is_displayed(ucs)))]


def server_addresses(address):
    """
    Return addresses of ``(host, port)``, of IPv4 first, as x/84 has
    always connected by IPv4: some networks ban IPv6 ranges of hosting.
    """
    infos = socket.getaddrinfo(address[0], address[1],
                               type=socket.SOCK_STREAM)
    return sorted(infos, key=lambda info: info[0] != socket.AF_INET)


def connect_socket(address):
    """ Return socket connected to ``(host, port)``, of the first address
    that connects within ``CONNECT_TIMEOUT``. """
    error = OSError('no address of {0}'.format(address[0]))
    for family, kind, proto, _, sockaddr in server_addresses(address):
        sock = socket.socket(family, kind, proto)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect(sockaddr)
        except OSError as err:
            sock.close()
            error = err
            continue
        return sock
    raise error


class InputLine(object):

    """ The line typed, with history of lines entered. """

    def __init__(self, max_length):
        self.text = u''
        self.max_length = max_length
        self.history = []
        self.hist_idx = None

    def keystroke(self, term, inp):
        """
        Edit the line of keystroke ``inp``.

        :returns: the line entered, by return; or whether the line changed.
        """
        if inp.code == term.KEY_ENTER or inp in (u'\r', u'\n'):
            line, self.text, self.hist_idx = self.text, u'', None
            if line.strip():
                self.history = (self.history + [line])[-50:]
            return line
        if inp.code in (term.KEY_BACKSPACE, term.KEY_DELETE) or inp in (
                u'\x08', u'\x7f'):
            self.text = self.text[:-1]
        elif inp == ctrl('w'):
            self.text = self.text.rstrip(u' ')
            self.text = self.text[:self.text.rfind(u' ') + 1]
        elif inp == ctrl('u'):
            self.text = u''
        elif inp.code == term.KEY_UP and self.history:
            self.hist_idx = (len(self.history) - 1 if self.hist_idx is None
                             else max(0, self.hist_idx - 1))
            self.text = self.history[self.hist_idx]
        elif inp.code == term.KEY_DOWN and self.hist_idx is not None:
            self.hist_idx += 1
            if self.hist_idx >= len(self.history):
                self.hist_idx, self.text = None, u''
            else:
                self.text = self.history[self.hist_idx]
        elif (not inp.is_sequence and len(inp) == 1 and is_displayed(inp)
              and len(self.text) < self.max_length):
            self.text += inp
        else:
            return False
        return True


class ChatScreen(object):

    """
    The screen of chat: messages, newest at the bottom, and the input line
    on the last row.

    New messages are written on the last row and scrolled up, as text is,
    so that only they are written, not the screen; the input line is then
    written again.  Each row of a message ends a cell short of the right
    margin, as some terminals scroll when it is written.
    """

    def __init__(self, term, encoding):
        self.term = term
        self.encoding = encoding
        self.entries = collections.deque(maxlen=MAX_SCROLLBACK)
        self.rows = []
        self.width = None
        #: rows scrolled back, from the newest.
        self.offset = 0
        self.input = InputLine(MAX_INPUT)
        self.prompt = u''
        self._output = []

    @property
    def height(self):
        """ Rows of messages. """
        return max(1, self.term.height - 1)

    def entry_rows(self, entry):
        """ Return rows displaying message ``entry``, (time, runs). """
        stamp, runs = entry
        width = self.width - INDENT
        rows = wrap_runs(runs, width) or [[]]
        stamp_runs = [(PLAIN._replace(fg=14), stamp.ljust(INDENT))]
        indent = [(PLAIN, u' ' * INDENT)]
        return [render_runs(self.term, (stamp_runs if idx == 0 else indent)
                            + row) for idx, row in enumerate(rows)]

    def layout(self):
        """ Measure the screen, rendering all messages again. """
        self.width = max(INDENT + 10, self.term.width - 1)
        self.rows = []
        for entry in self.entries:
            self.rows.extend(self.entry_rows(entry))
        self.offset = 0

    def add(self, runs):
        """ Add a message of ``runs``. """
        runs = [(style, fold_text(text, self.encoding))
                for style, text in runs]
        entry = (time.strftime('%H:%M'), runs)
        self.entries.append(entry)
        if self.width is None:
            return
        rows = self.entry_rows(entry)
        self.rows.extend(rows)
        excess = len(self.rows) - MAX_SCROLLBACK * 4
        if excess > 0:
            del self.rows[:excess]
        if self.offset:
            # the rows displayed remain, scrolled back.
            self.offset = min(len(self.rows) - self.height,
                              self.offset + len(rows))
        else:
            last = self.term.height - 1
            for row in rows:
                self._output.append(u''.join((
                    self.term.move(last, 0), self.term.normal,
                    self.term.clear_eol, row, u'\r\n')))

    def flush(self):
        """ Write messages added, and the input line, when any were. """
        if self._output:
            echo(u''.join(self._output) + self.input_line())
            del self._output[:]

    def scroll(self, pages):
        """ Scroll back by ``pages``, or forward, of a negative number. """
        most = max(0, len(self.rows) - self.height)
        offset = max(0, min(most, self.offset + pages * (self.height - 1)))
        if offset != self.offset:
            self.offset = offset
            self.redraw()

    def redraw(self):
        """ Write the screen. """
        if self.width is None or self.width != max(INDENT + 10,
                                                   self.term.width - 1):
            self.layout()
        del self._output[:]
        end = len(self.rows) - self.offset
        shown = self.rows[max(0, end - self.height):end]
        top = self.height - len(shown)
        echo(u''.join([self.term.normal, self.term.home, self.term.clear] + [
            self.term.move(top + idx, 0) + row
            for idx, row in enumerate(shown)]) + self.input_line())

    def input_line(self):
        """ Return sequence writing the input line, the cursor at its end. """
        term = self.term
        prompt = self.prompt
        if self.offset:
            prompt = u'{0} +{1}'.format(prompt, self.offset)
        prompt = u'[{0}] '.format(prompt)
        width = max(1, self.width - text_width(prompt))
        text = fold_text(fit_tail(self.input.text, width), self.encoding)
        color = getattr(term, COLOR_INPUTBAR)
        return u''.join((
            term.move(term.height - 1, 0), term.normal,
            color(prompt), text, term.clear_eol))


class Connection(irc.client.ServerConnection):

    """ A connection to an IRC server, tolerant of what it receives. """

    #: decode text of any encoding, rather than raising UnicodeDecodeError
    #: for that not of utf-8.
    buffer_class = LenientDecodingLineBuffer

    def _handle_other(self, arguments, command, source, tags):
        # a QUIT of no message raises IndexError (jaraco/irc#210).
        if command == 'quit' and not arguments:
            arguments = [u'']
        return super()._handle_other(arguments, command, source, tags)


class Reactor(irc.client.Reactor):

    """ Reactor of :class:`Connection`. """

    connection_class = Connection


class IRCChat(object):

    """
    IRC client of one channel, displaying events on a :class:`ChatScreen`.

    Events are handled by methods named ``on_<event>``, such as
    ``on_pubmsg``.  An error handling one is logged and displayed, rather
    than ending the chat.
    """

    # pylint: disable=R0904
    #         Too many public methods

    def __init__(self, term, session, screen):
        self.log = logging.getLogger(__name__)
        self.term = term
        self.session = session
        self.screen = screen
        self.reactor = Reactor()
        self.reactor.add_global_handler('all_events', self._dispatch, -10)
        self.connection = self.reactor.server()
        self.nick = None
        self.channel = None
        self.joining = None
        self.registered = False
        #: reason of disconnecting, once disconnected.
        self.closed = None
        self.quitting = False
        self.candidates = []
        self.names = []
        self._connect_error = None
        self.aborted = False

    # display

    def show(self, *runs):
        """ Display a message of ``runs``. """
        self.screen.add([run for part in runs for run in part])

    def system(self, template, *args):
        """ Display a message of the chat, of :func:`fmt`. """
        self.show(fmt(u'\x0314-*-\x0f '), fmt(template, *args))

    def update_prompt(self):
        """ Name the channel, or the nick, on the input line. """
        self.screen.prompt = self.channel or self.nick or SERVER

    # connecting

    def connect(self, handle):
        """
        Connect to the server, as ``handle``, until it is connected, it
        fails, or escape is pressed.

        :returns: whether it is connected.
        """
        self.candidates = nick_candidates(handle, MAX_NICK)
        self.nick = self.candidates.pop(0)
        self.update_prompt()
        self.system(u'Connecting to \x02{}\x02 port {} ... '
                    u'(press Escape to cancel)',
                    SERVER, str(PORT))
        self.screen.flush()
        thread = threading.Thread(target=self._connect, daemon=True)
        thread.start()
        while thread.is_alive():
            if self.wait_cancel(0.1):
                self.aborted = True
                return False
        if self._connect_error is not None:
            self.system(u'\x0304Could not connect:\x0f {}',
                        text_runs(str(self._connect_error)))
            return False
        return True

    def _connect(self):
        """ Connect, of a thread, as the connection may take time. """
        kwargs = {'connect_factory': self.open_socket}
        if SASL_USER and SASL_PASSWORD:
            kwargs.update(password=SASL_PASSWORD, sasl_login=SASL_USER)
        elif PASSWORD:
            kwargs['password'] = PASSWORD
        try:
            self.connection.connect(SERVER, PORT, self.nick,
                                    ircname=self.session.user.handle,
                                    **kwargs)
        except irc.client.ServerConnectionError as err:
            self._connect_error = err
            return
        if self.aborted:
            self.connection.disconnect()

    @staticmethod
    def open_socket(address):
        """ Return socket connected to ``address``, of ssl when enabled. """
        sock = connect_socket(address)
        try:
            if ENABLE_SSL:
                context = ssl.create_default_context()
                if not SSL_VERIFY:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                sock = context.wrap_socket(sock, server_hostname=address[0])
        except (OSError, ssl.SSLError):
            sock.close()
            raise
        # a send that cannot complete disconnects, rather than waiting.
        sock.settimeout(CONNECT_TIMEOUT * 2)
        return sock

    def wait_cancel(self, timeout):
        """ Whether escape (or /quit) is pressed, within ``timeout``. """
        event, data = self.session.read_events(('input', 'refresh'),
                                               timeout=timeout)
        if event == 'refresh':
            self.screen.redraw()
        elif event == 'input':
            self.session.buffer_input(data, pushback=True)
            inp = self.term.inkey(0)
            while inp:
                if is_escape(self.term, inp):
                    return True
                line = self.screen.input.keystroke(self.term, inp)
                if isinstance(line, str) and line.strip().lower() == u'/quit':
                    return True
                echo(self.screen.input_line())
                inp = self.term.inkey(0)
        return False

    # main loop

    def run(self):
        """ Chat until /quit, or disconnected, and a key is pressed. """
        deadline = time.time() + CONNECT_TIMEOUT * 2
        while True:
            self.process()
            if (not self.registered and self.closed is None and
                    time.time() > deadline):
                self.system(u'\x0304The server did not answer.')
                self.connection.disconnect(u'timeout')
                self.process()
            self.screen.flush()
            event, data = self.session.read_events(('input', 'refresh'),
                                                   timeout=0.05)
            if event == 'refresh':
                self.screen.redraw()
            elif event == 'input':
                self.session.buffer_input(data, pushback=True)
                if not self.handle_keys():
                    return

    def process(self):
        """ Handle what the server sent. """
        try:
            self.reactor.process_once(0)
        except Exception:
            # pylint: disable=W0703
            #         Catching too general exception
            self.log.exception('irc: processing')
            self.system(u'\x0304Error handling what the server sent.')

    def handle_keys(self):
        """ Handle keys pressed; returns False to end the chat. """
        term, screen = self.term, self.screen
        inp = term.inkey(0)
        while inp:
            if self.closed is not None:
                # disconnected: return or escape leaves.
                if is_escape(term, inp) or inp.code == term.KEY_ENTER or (
                        inp in (u'\r', u'\n')):
                    return False
            elif inp.code == term.KEY_PGUP:
                screen.scroll(1)
            elif inp.code == term.KEY_PGDOWN:
                screen.scroll(-1)
            elif inp == ctrl('l'):
                screen.redraw()
            elif is_escape(term, inp):
                screen.input.text = u''
                echo(screen.input_line())
            else:
                line = screen.input.keystroke(term, inp)
                if isinstance(line, str):
                    if not self.command(line):
                        return False
                    screen.flush()
                    echo(screen.input_line())
                elif line:
                    echo(screen.input_line())
            inp = term.inkey(0)
        return True

    def close(self):
        """ Quit irc, and disconnect. """
        self.aborted = True
        try:
            if self.connection.is_connected():
                if not self.quitting:
                    self.connection.quit(QUIT_MESSAGE)
                self.connection.disconnect()
        except Exception:
            # pylint: disable=W0703
            #         Catching too general exception
            self.log.exception('irc: disconnecting')

    # commands

    def command(self, line):
        """ Perform ``line`` typed; returns False to end the chat. """
        if not line.strip():
            return True
        if line.startswith(u'/') and not line.startswith(u'//'):
            name, _, rest = line[1:].partition(u' ')
            name, rest = name.lower(), rest.strip()
            if name == u'quit':
                self.quitting = True
                self.connection.quit(rest or QUIT_MESSAGE)
                return False
            method = getattr(self, 'cmd_' + name, None)
            if method is None:
                self.system(u'Unknown command \x02/{}\x02; type \x02/help'
                            u'\x02 for commands.', name)
            elif not self.registered and name not in ('help', 'colors',
                                                     'clear'):
                self.system(u'Not connected yet.')
            else:
                method(rest)
            return True
        if line.startswith(u'//'):
            line = line[1:]
        if not self.registered:
            self.system(u'Not connected yet.')
        elif self.channel is None:
            self.system(u'You are not in a channel: \x02/join #channel'
                        u'\x02 to join one.')
        else:
            for text in split_message(mirc_encode(line)):
                self.connection.privmsg(self.channel, text)
                self.show(fmt(u'<{}> ', nick_runs(
                    self.nick, PLAIN._replace(bold=True))), text_runs(text))
        return True

    def cmd_help(self, _):
        """ /help: display commands. """
        for usage, text in (
                (u'/join #channel', u'join a channel, leaving this one'),
                (u'/part [message]', u'leave this channel'),
                (u'/me action', u'describe what you are doing'),
                (u'/msg nick message', u'send a private message'),
                (u'/nick newnick', u'change your nick'),
                (u'/topic [new topic]', u'display or change the topic'),
                (u'/names', u'list who is in this channel'),
                (u'/whois nick', u'tell about a user'),
                (u'/away [message]', u'mark yourself away, or back'),
                (u'/colors', u'display pipe codes of colors'),
                (u'/clear', u'clear the screen'),
                (u'/quit [message]', u'leave the chat')):
            self.system(u'\x02{}\x02  {}', usage.ljust(19),
                        text)
        self.system(u'PgUp and PgDn scroll back through messages, and Up '
                    u'and Down recall lines typed.')

    def cmd_colors(self, _):
        """ /colors: display pipe codes. """
        self.system(u'Color messages by pipe codes, such as |04:')
        self.system(u''.join(u'\x03{0:02d}|{1:02d}\x0f '.format(
            PIPE_MIRC[num], num) for num in range(16)))
        self.system(u''.join(u'\x0300,{0:02d}|b{1}\x0f '.format(
            PIPE_MIRC[num], num) for num in range(8)))
        self.system(u'\x02|bb bold\x0f \x1f|uu underline\x0f '
                    u'\x16|rr reverse\x0f |nn normal')

    def cmd_clear(self, _):
        """ /clear: clear the screen. """
        self.screen.entries.clear()
        self.screen.layout()
        self.screen.redraw()

    def cmd_me(self, rest):
        """ /me action: describe an action. """
        if not rest:
            self.system(u'Usage: \x02/me action')
        elif self.channel is None:
            self.system(u'You are not in a channel.')
        else:
            for text in split_message(mirc_encode(rest)):
                self.connection.action(self.channel, text)
                self.show(fmt(u'\x02*\x02 {} ', nick_runs(
                    self.nick, PLAIN._replace(bold=True))), text_runs(text))

    def cmd_msg(self, rest):
        """ /msg nick message: send a private message. """
        target, _, text = rest.partition(u' ')
        if not target or not text.strip():
            self.system(u'Usage: \x02/msg nick message')
            return
        for part in split_message(mirc_encode(text.strip())):
            self.connection.privmsg(target, part)
            self.show(fmt(u'\x0313->\x0f \x02*{}*\x02 ', target),
                      text_runs(part))

    def cmd_nick(self, rest):
        """ /nick newnick: change nick. """
        if not rest or u' ' in rest:
            self.system(u'Usage: \x02/nick newnick')
        else:
            self.connection.nick(rest)

    def cmd_join(self, rest):
        """ /join #channel [key]: join a channel, leaving this one. """
        channel, _, key = rest.partition(u' ')
        if not channel:
            self.system(u'Usage: \x02/join #channel')
            return
        if not irc.client.is_channel(channel):
            channel = u'#' + channel
        if self.channel and channel.lower() == self.channel.lower():
            self.system(u'You are in {} already.', channel)
            return
        self.joining = channel
        self.connection.join(channel, key.strip())

    def cmd_part(self, rest):
        """ /part [message]: leave this channel. """
        if self.channel is None:
            self.system(u'You are not in a channel.')
        else:
            self.connection.part(self.channel, rest)

    def cmd_topic(self, rest):
        """ /topic [new topic]: display or change the topic. """
        if self.channel is None:
            self.system(u'You are not in a channel.')
        else:
            self.connection.topic(self.channel, mirc_encode(rest) or None)

    def cmd_names(self, _):
        """ /names: list who is in this channel. """
        if self.channel is None:
            self.system(u'You are not in a channel.')
        else:
            self.connection.names([self.channel])

    def cmd_whois(self, rest):
        """ /whois nick: tell about a user. """
        if not rest:
            self.system(u'Usage: \x02/whois nick')
        else:
            self.connection.whois(rest.split()[0])

    def cmd_away(self, rest):
        """ /away [message]: mark away, or back, of none. """
        if rest:
            self.connection.send_items(u'AWAY', u':' + rest)
        else:
            self.connection.send_items(u'AWAY')

    # events of irc

    def _dispatch(self, connection, event):
        """ Handle ``event`` by its method, should there be one. """
        method = getattr(self, 'on_' + event.type, None)
        if method is None and event.type in ERROR_EVENTS:
            method = self.on_error_reply
        if method is None:
            return
        try:
            method(connection, event)
        except Exception:
            # pylint: disable=W0703
            #         Catching too general exception
            self.log.exception('irc: event {0!r}'.format(event))
            self.system(u'\x0304Error displaying an event of type {}.',
                        event.type)

    @staticmethod
    def arg(event, idx, default=u''):
        """ Return argument ``idx`` of ``event``, or ``default``. """
        return (event.arguments[idx] if len(event.arguments) > idx
                else default)

    def on_welcome(self, connection, _):
        """ Registered: join the channel. """
        self.registered = True
        self.nick = connection.get_nickname()
        self.update_prompt()
        self.system(u'Connected to \x02{}\x02 as \x02{}\x02.  Type '
                    u'\x02/help\x02 for commands.', SERVER,
                    nick_runs(self.nick))
        if irc.client.is_channel(CHANNEL):
            self.joining = CHANNEL
            connection.join(CHANNEL)

    def on_nicknameinuse(self, connection, event):
        """ A nick refused: try another, connecting. """
        refused = self.arg(event, 0)
        if self.registered:
            self.system(u'\x0304{}:\x0f {}', refused,
                        text_runs(self.arg(event, 1)))
        elif self.candidates:
            self.nick = self.candidates.pop(0)
            self.system(u'Nick {} is not available, trying {}.',
                        refused, self.nick)
            connection.nick(self.nick)
        else:
            self.system(u'\x0304No nick is available.')
            connection.disconnect(u'no nick available')

    on_erroneusnickname = on_nickcollision = on_nicknameinuse
    on_unavailresource = on_nicknameinuse

    def on_disconnect(self, _, event):
        """ Disconnected. """
        if self.closed is None:
            self.closed = self.arg(event, 0) or u'disconnected'
            self.system(u'\x0304Disconnected\x0f ({}).  Press Return to '
                        u'leave.', text_runs(self.closed))

    def on_error(self, _, event):
        """ The server closes the connection, of ERROR. """
        self.system(u'\x0304Error:\x0f {}', text_runs(event.target or
                                                      self.arg(event, 0)))

    def on_error_reply(self, _, event):
        """ A numeric reply of an error, such as no such nick. """
        args = list(event.arguments)
        if args and args[0] == self.joining:
            self.joining = None
        self.system(u'\x0304{}', text_runs(u': '.join(
            arg for arg in args if arg)))

    def on_login_failed(self, _, event):
        """ SASL login failed. """
        self.system(u'\x0304Login failed:\x0f {}', text_runs(
            self.arg(event, 0)))

    def on_loggedin(self, _, event):
        """ SASL login succeeded. """
        self.system(u'{}', text_runs(event.arguments[-1]))

    on_unaway = on_nowaway = on_loggedin

    def on_join(self, connection, event):
        """ Someone joined; or we did, leaving the channel we were in. """
        nick, channel = event.source.nick, event.target
        if nick == self.nick:
            previous, self.channel = self.channel, channel
            self.joining = None
            self.update_prompt()
            self.system(u'You joined \x02{}\x02.', channel)
            if previous and previous.lower() != channel.lower():
                connection.part(previous)
        elif channel == self.channel:
            self.system(u'\x0303{}\x0f ({}) joined {}.', nick_runs(nick),
                        event.source.userhost,
                        channel)

    def on_part(self, _, event):
        """ Someone left a channel, or we did. """
        nick, channel = event.source.nick, event.target
        if nick == self.nick:
            self.system(u'You left {}.', channel)
            if channel == self.channel:
                self.channel = None
                self.update_prompt()
        elif channel == self.channel:
            self.system(u'\x0305{}\x0f left {}{}.', nick_runs(nick),
                        channel, reason_runs(self.arg(event, 0)))

    def on_kick(self, _, event):
        """ Someone was kicked, or we were. """
        victim, reason = self.arg(event, 0), self.arg(event, 1)
        self.system(u'\x0304{}\x0f was kicked from {} by {}{}.',
                    nick_runs(victim), event.target,
                    nick_runs(event.source.nick), reason_runs(reason))
        if victim == self.nick and event.target == self.channel:
            self.channel = None
            self.update_prompt()
            self.system(u'\x02/join {}\x02 to join again.',
                        event.target)

    def on_quit(self, _, event):
        """ Someone quit IRC. """
        self.system(u'\x0305{}\x0f quit{}.', nick_runs(event.source.nick),
                    reason_runs(self.arg(event, 0)))

    def on_nick(self, _, event):
        """ Someone changed their nick, or we did. """
        old, new = event.source.nick, event.target
        if old == self.nick:
            self.nick = new
            self.update_prompt()
            self.system(u'You are now known as \x02{}\x02.', nick_runs(new))
        else:
            self.system(u'{} is now known as {}.', nick_runs(old),
                        nick_runs(new))

    def on_mode(self, _, event):
        """ Modes of the channel changed. """
        if event.target == self.channel:
            self.system(u'{} sets mode {}.', nick_runs(event.source.nick),
                        text_runs(u' '.join(event.arguments)))

    def on_currenttopic(self, _, event):
        """ The topic, of joining or asking. """
        self.system(u'Topic of {}: {}', self.arg(event, 0),
                    text_runs(self.arg(event, 1)))

    def on_notopic(self, _, event):
        """ No topic. """
        self.system(u'{} has no topic.', self.arg(event, 0))

    def on_topic(self, _, event):
        """ Someone changed the topic. """
        self.system(u'{} changed the topic of {} to: {}',
                    nick_runs(event.source.nick), event.target,
                    text_runs(self.arg(event, 0)))

    def on_namreply(self, _, event):
        """ Nicks of a channel, of several replies. """
        self.names.extend(self.arg(event, 2).split())

    def on_endofnames(self, _, event):
        """ The nicks of a channel, all of them. """
        names, self.names = self.names, []
        rank = {u'~': 0, u'&': 1, u'@': 2, u'%': 3, u'+': 4}
        names.sort(key=lambda name: (rank.get(name[:1], 5), name.lower()))
        self.system(u'In {} ({}): {}', self.arg(event, 0),
                    str(len(names)),
                    u' '.join(names) or u'nobody')

    def on_pubmsg(self, _, event):
        """ A message of a channel. """
        text = self.arg(event, 0)
        runs = text_runs(text)
        style = PLAIN._replace(fg=nick_color(event.source.nick))
        if mentions(plain_text(runs), self.nick):
            style = style._replace(reverse=True)
        channel = (u'' if event.target == self.channel
                   else u'{0} '.format(event.target))
        self.show(fmt(u'{}<{}> ', channel,
                      nick_runs(event.source.nick, style)), runs)

    def on_privmsg(self, _, event):
        """ A private message. """
        self.show(fmt(u'\x0313*{}*\x0f ', event.source.nick),
                  text_runs(self.arg(event, 0)))

    def on_action(self, _, event):
        """ An action, of /me, of a channel or private. """
        private = u'' if irc.client.is_channel(event.target) else u'(private) '
        self.show(fmt(u'\x02*\x02 {}{} ', private,
                      nick_runs(event.source.nick)),
                  text_runs(self.arg(event, 0)))

    def on_pubnotice(self, _, event):
        """ A notice of a channel. """
        self.show(fmt(u'\x0313-{}:{}-\x0f ', event.source.nick,
                      event.target),
                  text_runs(self.arg(event, 0)))

    def on_privnotice(self, _, event):
        """ A notice to us, of a user, or of the server. """
        source = event.source or u''
        if u'!' not in source:
            # of the server, such as of connecting.
            if ENABLE_PRIVNOTICE:
                self.system(u'{}', text_runs(self.arg(event, 0)))
            return
        self.show(fmt(u'\x0313-{}-\x0f ', event.source.nick),
                  text_runs(self.arg(event, 0)))

    def on_ctcp(self, connection, event):
        """ Answer CTCP VERSION and PING. """
        command = self.arg(event, 0).upper()
        if command == u'VERSION':
            connection.ctcp_reply(event.source.nick,
                                  u'VERSION {0}'.format(VERSION))
        elif command == u'PING':
            connection.ctcp_reply(event.source.nick, u'PING {0}'.format(
                self.arg(event, 1)).strip())

    def on_away(self, _, event):
        """ A user messaged is away. """
        self.system(u'{} is away: {}', nick_runs(self.arg(event, 0)),
                    text_runs(self.arg(event, 1)))

    def on_whoisuser(self, _, event):
        """ Of /whois: a user's address and name. """
        self.system(u'{} is {}@{} ({})', nick_runs(self.arg(event, 0)),
                    self.arg(event, 1),
                    self.arg(event, 2),
                    text_runs(self.arg(event, 4)))

    def on_whoisserver(self, _, event):
        """ Of /whois: a user's server. """
        self.system(u'{} is on {} ({})', nick_runs(self.arg(event, 0)),
                    self.arg(event, 1),
                    text_runs(self.arg(event, 2)))

    def on_whoischannels(self, _, event):
        """ Of /whois: a user's channels. """
        self.system(u'{} is in {}', nick_runs(self.arg(event, 0)),
                    text_runs(self.arg(event, 1)))

    def on_whoisidle(self, _, event):
        """ Of /whois: how long a user has been idle. """
        seconds = int(self.arg(event, 1, u'0') or 0)
        self.system(u'{} has been idle {}m {}s', nick_runs(self.arg(event, 0)),
                    str(seconds // 60),
                    str(seconds % 60))

    def on_whoisaccount(self, _, event):
        """ Of /whois: a user's account. """
        self.system(u'{} is logged in as {}', nick_runs(self.arg(event, 0)),
                    self.arg(event, 1))


def main():
    """ x/84 script launch point """
    term, session = getterminal(), getsession()
    session.activity = u'Chatting on IRC'

    # set font
    if SYNCTERM_FONT and term.kind.startswith('ansi'):
        echo(syncterm_setfont(SYNCTERM_FONT))

    screen = ChatScreen(term, session.encoding)
    chat = IRCChat(term, session, screen)
    screen.redraw()
    try:
        if chat.connect(session.user.handle):
            chat.run()
        else:
            chat.screen.flush()
            if chat.closed is None and not chat.aborted:
                term.inkey(5)
    finally:
        chat.close()
        echo(term.normal + term.clear)
