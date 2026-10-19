"""
Multi Relay Chat (MRC) script for x/84.

MRC is a chat of BBSes, of rooms, by the bbs's connection to the MRC
server, of section ``[mrc]`` (see :mod:`x84.mrc`).  The screen is the
room's name and topic on the top row, its messages below them, newest at
the bottom, and the input line on the last row: type to say something,
or a command, such as ``/?`` for those of this client, or ``/help`` for
those of the server.  Page Up and Page Down scroll back through messages,
Left and Right change the color of what you say, and Tab completes a
nick.

Messages are colored by pipe codes, such as ``|04``; any control
character of a message, such as of an escape sequence, is discarded.

Options of section ``[mrc]``, besides those of the connection:

- ``room``: the room first joined, ``lobby``; a user may choose their own
  by ``/set defaultroom``.
- ``max_scrollback``: messages kept to scroll back to, ``200``.
"""
# std imports
import codecs
import collections
import logging
import re
import time
import unicodedata

# local
from x84.bbs import getsession, getterminal, echo, get_ini
from x84.bbs.textbuffer import char_width, text_width, wrap_offsets
from x84.mrc import (Packet, RESERVED, format_packet, parse_packet,
                     name_tag, site_tag, strip_pipes, ascii_text)
from editor import ctrl, is_escape

#: how many messages of scrollback to keep.
MAX_SCROLLBACK = get_ini('mrc', 'max_scrollback', getter='getint') or 200

#: the room first joined.
DEFAULT_ROOM = get_ini('mrc', 'room') or u'lobby'

#: the longest line typed, of the protocol.
MAX_INPUT = 140

#: the longest packet, of the protocol.
MAX_PACKET = 255

#: longest name of a room, and topic.
MAX_ROOM = 20
MAX_TOPIC = 55

#: the room of CTCP requests and replies.
CTCP_ROOM = u'ctcp_echo_channel'

#: seconds between requests of the room's users, between telling the
#: server the user is here, and between requests of latency.
USERLIST_INTERVAL = 20
ALIVE_INTERVAL = 120
LATENCY_INTERVAL = 10

#: cells of the time that begins each message.
INDENT = 6

#: terminal colors of the pipe colors of DOS, 0 to 7.
DOS_ANSI = (0, 4, 2, 6, 1, 5, 3, 7)

#: commands of the server, of which the second word is a password.
MASKED = {u'/identify': 1, u'/register': 1, u'/roompass': 1,
          u'/update': 2, u'/roomconfig': 2}

RE_PIPE = re.compile(r'\|([0-9]{2})')

#: characters of direction, which would reorder the screen.
BIDI = frozenset(chr(code) for code in (
    0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e,
    0x2066, 0x2067, 0x2068, 0x2069))

#: what a user may /set, and its default.
PREFERENCES = collections.OrderedDict((
    ('defaultroom', DEFAULT_ROOM),
    ('nickcolor', u'|11'),
    ('ltbracket', u'|03<'),
    ('rtbracket', u'|03>'),
    ('enterchat', u'|07- |11%1 |03has arrived!'),
    ('leavechat', u'|07- |12%1 |04has left chat.'),
    ('enterroom', u'|07- |11%1 |03has entered the room.'),
    ('leaveroom', u'|07- |02%1 |10has left the room.'),
))

#: what each preference is, of /set.
PREFERENCE_HELP = {
    'defaultroom': u'the room joined on entering chat',
    'nickcolor': u'the pipe code of the color of your nick',
    'ltbracket': u'the bracket left of your nick, of pipe codes',
    'rtbracket': u'the bracket right of your nick, of pipe codes',
    'enterchat': u'told others as you enter chat; %1 is your nick',
    'leavechat': u'told others as you leave chat',
    'enterroom': u'told a room as you enter it',
    'leaveroom': u'told a room as you leave it',
}

#: style of text: ``fg`` is a pipe color, 0 to 15, ``bg`` 0 to 7, or None.
Style = collections.namedtuple('Style', ['fg', 'bg'])

PLAIN = Style(None, None)


def is_displayed(ucs):
    """ Whether character ``ucs`` of text received may be displayed. """
    code = ord(ucs)
    return not (code < 0x20 or 0x7f <= code < 0xa0 or ucs in BIDI or
                code in (0x2028, 0x2029))


def parse_pipes(text, style=PLAIN):
    """
    Return runs of ``(style, text)`` of ``text`` colored by pipe codes:
    ``|00`` to ``|15`` are colors of text, ``|16`` to ``|23`` of its
    background, and ``|24`` to ``|31`` those of a bright background.
    """
    runs, pos = [], 0
    for match in RE_PIPE.finditer(text):
        code = int(match.group(1))
        if code > 31:
            continue
        if match.start() > pos:
            runs.append((style, text[pos:match.start()]))
        if code < 16:
            style = style._replace(fg=code)
        else:
            style = style._replace(bg=code % 8)
        pos = match.end()
    runs.append((style, text[pos:]))
    return [(_style, u''.join(ucs for ucs in part if is_displayed(ucs)))
            for _style, part in runs if part]


def style_sequence(term, style):
    """ Return sequence of the terminal displaying ``style``. """
    parts = [term.normal]
    fg, bg = style
    if fg is not None:
        color = DOS_ANSI[fg % 8]
        if fg < 8:
            parts.append(term.color(color))
        elif term.number_of_colors >= 16:
            parts.append(term.color(color + 8))
        else:
            # bright, as of an ansi terminal, by bold; and of the bright
            # color of most others, of 8 colors by their terminfo, such as
            # xterm-color, which display bold of the normal color.
            parts.extend((term.bold, term.color(color)))
            if not (term.kind or u'').startswith('ansi'):
                parts.append(u'\x1b[{0}m'.format(90 + color))
    if bg:
        parts.append(term.on_color(DOS_ANSI[bg]))
    return u''.join(parts)


def is_hidden(style):
    """
    Whether text of ``style`` is hidden: black, of a black background, as
    some servers and clients write in place of spaces they would lose.
    """
    return style.fg == 0 and not style.bg


def plain_text(runs):
    """ Return the text of ``runs``. """
    return u''.join(text for _, text in runs)


def is_utf8(encoding):
    """ Whether ``encoding`` is utf-8. """
    try:
        return codecs.lookup(encoding).name == 'utf-8'
    except LookupError:
        return False


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


def fit_runs(runs, width):
    """ Return the beginning of ``runs`` that fits ``width`` cells. """
    result, cells = [], 0
    for style, text in runs:
        part = u''
        for ucs in text:
            if cells + char_width(ucs) > width:
                break
            part += ucs
            cells += char_width(ucs)
        if part:
            result.append((style, part))
        if len(part) < len(text):
            break
    return result


def mask_input(text):
    """ Return ``text`` typed, of its password masked, of :data:`MASKED`. """
    words = text.split(u' ')
    idx = MASKED.get(words[0].lower())
    if idx == 2 and (len(words) < 2 or words[1].lower() != u'password'):
        idx = None
    if idx is not None and len(words) > idx:
        words[idx] = u'*' * len(words[idx])
    return u' '.join(words)


def mentions(body, nick):
    """ Whether message ``body`` names ``nick``, but for its first word. """
    rest = strip_pipes(body).split(None, 1)[1:]
    return bool(rest and re.search(
        r'(?<![A-Za-z0-9_]){0}(?![A-Za-z0-9_])'.format(re.escape(nick)),
        rest[0], re.IGNORECASE))


def room_name(text):
    """ Return name of room ``text``, as of the protocol. """
    return name_tag(text.strip().lstrip(u'#')).lower()


def expand(template, nick, new_room=u'', old_room=u''):
    """ Return message ``template`` of /set, of its nick and rooms. """
    return (template.replace(u'%1', nick).replace(u'%2', u'')
            .replace(u'%3', u'#' + new_room).replace(u'%4', u'#' + old_room))


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
    The screen of chat: the room on the top row, messages below it, newest
    at the bottom, and the input line on the last row.

    New messages are written on the last row and scrolled up, as text is,
    so that only they are written, not the screen; the top row and input
    line are then written again.  Each row of a message ends a cell short
    of the right margin, as some terminals scroll when it is written.
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
        self.prompt_style = None
        self.text_style = PLAIN
        #: runs of the top row, and of its right end.
        self.title = []
        self.status = u''
        self._output = []

    @property
    def height(self):
        """ Rows of messages. """
        return max(1, self.term.height - 2)

    def entry_rows(self, entry):
        """ Return rows displaying message ``entry``, (time, runs, mark). """
        stamp, runs, mark = entry
        width = self.width - INDENT
        rows = wrap_runs(runs, width) or [[]]
        stamp_style = Style(15, 4) if mark else Style(8, None)
        stamp_runs = [(stamp_style, stamp), (PLAIN, u' ')]
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

    def add(self, runs, mark=False):
        """ Add a message of ``runs``, its time marked, of a mention. """
        runs = [(PLAIN, u' ' * text_width(text)) if is_hidden(style)
                else (style, fold_text(text, self.encoding))
                for style, text in runs]
        entry = (time.strftime('%H:%M'), runs, mark)
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
        """ Write messages added, the top row and the input line. """
        if self._output:
            echo(u''.join(self._output) + self.top_row() + self.input_line())
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
        top = 1 + self.height - len(shown)
        echo(u''.join([self.term.normal, self.term.home, self.term.clear] + [
            self.term.move(top + idx, 0) + row
            for idx, row in enumerate(shown)]) +
            self.top_row() + self.input_line())

    def top_row(self):
        """ Return sequence writing the top row: the room, and status. """
        term = self.term
        status = fold_text(self.status, self.encoding)
        width = max(1, self.width - text_width(status) - 1)
        title = fit_runs([(style, fold_text(text, self.encoding))
                          for style, text in self.title], width)
        pad = u' ' * max(0, self.width - text_width(plain_text(title)) -
                         text_width(status))
        return u''.join((term.move(0, 0), render_runs(term, title), pad,
                         render_runs(term, [(Style(8, None), status)]),
                         term.clear_eol))

    def input_line(self):
        """ Return sequence writing the input line, the cursor at its end. """
        term = self.term
        prompt = self.prompt
        if self.offset:
            prompt = u'{0} +{1}'.format(prompt, self.offset)
        prompt = u'[{0}] '.format(prompt)
        width = max(1, self.width - text_width(prompt))
        text = fold_text(fit_tail(mask_input(self.input.text), width),
                         self.encoding)
        color = (term.reverse if self.prompt_style is None
                 else style_sequence(term, self.prompt_style))
        return u''.join((
            term.move(term.height - 1, 0), term.normal, color, prompt,
            term.normal, style_sequence(term, self.text_style), text,
            term.normal, term.clear_eol))


class MRCChat(object):

    """
    Client of MRC, of the bbs's relay, displaying a room on a
    :class:`ChatScreen`.

    Packets are passed by events ``mrc`` of the engine; see :mod:`x84.mrc`.
    """

    # pylint: disable=R0902,R0904
    #         Too many instance attributes, public methods

    def __init__(self, term, session, screen):
        self.log = logging.getLogger(__name__)
        self.term = term
        self.session = session
        self.screen = screen
        self.nick = name_tag(session.user.handle)
        self.site = site_tag()
        self.prefs = dict(PREFERENCES)
        self.prefs.update(session.user.get('mrc', {}))
        self.text_color = int(session.user.get('mrc_textcolor', 7))
        self.twits = list(session.user.get('mrc_twits', []))
        self.room = u''
        self.topic = u''
        self.users = []
        self.latency = None
        self.state = None
        self.joined = False
        self.announced = False
        self.last_direct = None
        self.timers = {}
        self.completion = None
        self.screen.text_style = Style(self.text_color, None)
        self.update_top()

    # display

    def show(self, body, mark=False):
        """ Display message ``body``, of pipe codes. """
        self.screen.add(parse_pipes(body), mark)

    def show_literal(self, *runs):
        """ Display a message of ``runs``, of pipe codes displayed. """
        self.screen.add(list(runs))

    def system(self, text):
        """ Display a message of this client. """
        self.show(u'|15* |11' + text)

    def error(self, text):
        """ Display an error. """
        self.show(u'|15! |12' + text)

    def update_top(self):
        """ Update the top row and prompt, of the room. """
        room = u'#' + self.room if self.room else u'MRC'
        self.screen.title = ([(Style(11, None), room)] +
                             ([(PLAIN, u'  ')] + parse_pipes(
                                 self.topic, Style(7, None))
                              if self.topic else []))
        status = []
        if self.state != 'connected':
            status.append(self.state or u'connecting')
        else:
            status.append(u'{0} users'.format(len(self.users)))
            if self.latency is not None:
                status.append(u'{0}ms'.format(self.latency))
        self.screen.status = u' '.join(status)
        self.update_prompt()

    def update_prompt(self):
        """ Name the room on the input line, or the user written to. """
        target = self.direct_target(self.screen.input.text)
        if target:
            self.screen.prompt = u'-> ' + target
            self.screen.prompt_style = Style(15, 4)
        else:
            self.screen.prompt = u'#' + self.room if self.room else self.nick
            self.screen.prompt_style = None
        self.screen.input.max_length = self.max_input()

    @staticmethod
    def direct_target(text):
        """ The user of a direct message being typed, or None. """
        words = text.split(u' ')
        if len(words) > 2 and words[0].lower() in (
                u'/t', u'/tell', u'/m', u'/msg', u'/pm'):
            return words[1].upper()
        return None

    def name_prompt(self):
        """ Return what begins each line said: the nick, bracketed. """
        return u'{0}{1}{2}{3}|16|07 '.format(
            self.prefs['ltbracket'], self.prefs['nickcolor'], self.nick,
            self.prefs['rtbracket'])

    def max_input(self):
        """ The longest line typed, of the packet it is sent by. """
        prefix = (len(self.nick) + len(self.site) + len(self.room) * 2 +
                  len(self.name_prompt()) + 20)
        return max(20, min(MAX_INPUT, MAX_PACKET - prefix))

    # packets sent

    def send(self, to_user, msg_ext, to_room, body, from_room=None):
        """ Send packet of ``body``, from this user. """
        packet = Packet(self.nick, self.site,
                        self.room if from_room is None else from_room,
                        to_user, msg_ext, to_room, body)
        self.session.send_event('mrc', ('send', format_packet(packet)))

    def send_room(self, body):
        """ Say ``body`` to the room. """
        self.send(u'', u'', self.room, body)

    def send_room_notme(self, body, room=None):
        """ Say ``body`` to the room, or ``room``, but to this user. """
        room = self.room if room is None else room
        self.send(u'NOTME', u'', room, body, from_room=room)

    def send_all_notme(self, body):
        """ Say ``body`` to every room, but to this user. """
        self.send(u'NOTME', u'', u'', body)

    def send_server(self, body):
        """ Send command ``body`` to the server. """
        self.send(u'SERVER', self.site, self.room, body)

    def send_client(self, body):
        """ Send command ``body`` to the bbs's relay. """
        self.send(u'CLIENT', self.site, self.room, body)

    def send_ctcp(self, to_user, kind, text):
        """ Send CTCP ``kind`` of ``text`` to ``to_user``. """
        self.send(to_user, u'', CTCP_ROOM, u'{0} {1} {2}'.format(
            kind, self.nick, text), from_room=CTCP_ROOM)

    # entering and leaving

    def enter(self):
        """ Enter chat; returns False if it is not available. """
        if self.nick.upper() in RESERVED or not self.nick:
            self.error(u'Your handle may not be used on MRC.')
            return False
        # what remains of a chat before, as of a disconnected relay.
        self.session.flush_event('mrc')
        self.session.send_event('mrc', ('join', self.nick))
        self.joined = True
        self.system(u'Welcome to Multi Relay Chat, a chat of BBSes.')
        self.system(u'Type |15/? |11for commands, |15/help |11for those of '
                    u'the server, and |15/quit |11to leave.')
        event, data = self.session.read_events(('mrc',), timeout=10)
        if event is None:
            self.error(u'The bbs did not answer.')
            return False
        self.handle_event(data)
        if self.state == 'disabled':
            self.error(u'Multi Relay Chat is not available on this bbs.')
            return False
        return True

    def announce(self):
        """ Tell the server and others this user is here. """
        first = not self.announced
        self.announced = True
        if first:
            self.show(u'|07- |15You have entered chat')
        self.send_all_notme(expand(self.prefs['enterchat'], self.nick))
        self.send_server(u'IAMHERE')
        self.send_server(u'TERMSIZE:{0}x{1}'.format(
            self.term.width, self.term.height))
        self.send_server(u'BBSMETA: SecLevel({0}) SysOp({1})'.format(
            255 if self.session.user.is_sysop else 10,
            name_tag(get_ini('system', 'sysop') or u'')))
        self.send_client(u'LATENCY')
        room, self.room = self.room or room_name(
            self.prefs['defaultroom']) or DEFAULT_ROOM, u''
        self.join_room(room, announce=False)
        now = time.time()
        self.timers = {'userlist': now + 1, 'alive': now + ALIVE_INTERVAL,
                       'latency': now + LATENCY_INTERVAL}

    def leave(self, message=u''):
        """ Leave chat, of a quit ``message``. """
        if not self.joined:
            return
        self.joined = False
        try:
            if self.announced and self.state == 'connected':
                self.show(u'|07- |12You have left chat.')
                if message:
                    self.send_all_notme(
                        u'|07- |12{0}|04 has left chat |07(|15{1}|07)'
                        .format(self.nick, message))
                else:
                    self.send_all_notme(
                        expand(self.prefs['leavechat'], self.nick))
                self.send_server(u'LOGOFF')
            self.session.send_event('mrc', ('leave',))
        finally:
            self.save()

    def save(self):
        """ Save this user's preferences. """
        user = self.session.user
        changed = {key: value for key, value in self.prefs.items()
                   if value != PREFERENCES[key]}
        if changed != user.get('mrc', {}):
            user['mrc'] = changed
        if self.text_color != user.get('mrc_textcolor', 7):
            user['mrc_textcolor'] = self.text_color
        if self.twits != user.get('mrc_twits', []):
            user['mrc_twits'] = self.twits

    def join_room(self, room, announce=True):
        """ Move to ``room``, telling the rooms of it, of ``announce``. """
        old = self.room
        self.send_server(u'NEWROOM:{0}:{1}'.format(old, room))
        if announce:
            self.show(u'|07- |10You have left room |02#' + old)
            self.send_room_notme(expand(
                self.prefs['leaveroom'], self.nick, room, old), old)
        self.room, self.topic, self.users = room, u'', []
        if announce:
            self.show(u'|07- |11Joining room |02#' + room)
            self.send_room_notme(expand(
                self.prefs['enterroom'], self.nick, room, old))
        self.send_server(u'USERLIST')
        self.update_top()

    # main loop

    def run(self):
        """ Chat until /quit. """
        while True:
            self.tick()
            self.screen.flush()
            event, data = self.session.read_events(
                ('input', 'refresh', 'mrc'), timeout=1)
            if event == 'refresh':
                self.screen.redraw()
            elif event == 'input':
                self.session.buffer_input(data, pushback=True)
                if not self.handle_keys():
                    return
            elif event == 'mrc':
                self.handle_event(data)
                # handle all received, before writing the screen.
                while True:
                    data = self.session.read_event('mrc', timeout=-1)
                    if data is None:
                        break
                    self.handle_event(data)

    def tick(self):
        """ Send what is sent from time to time. """
        if self.state != 'connected' or not self.announced:
            return
        now = time.time()
        if now >= self.timers['userlist']:
            self.send_server(u'USERLIST')
            self.timers['userlist'] = now + USERLIST_INTERVAL
        if now >= self.timers['alive']:
            self.send_server(u'IAMHERE')
            self.timers['alive'] = now + ALIVE_INTERVAL
        if now >= self.timers['latency']:
            self.send_client(u'LATENCY')
            self.timers['latency'] = now + LATENCY_INTERVAL

    def handle_keys(self):
        """ Handle keys pressed; returns False to end the chat. """
        term, screen = self.term, self.screen
        inp = term.inkey(0)
        while inp:
            if inp.code == term.KEY_PGUP:
                screen.scroll(1)
            elif inp.code == term.KEY_PGDOWN:
                screen.scroll(-1)
            elif inp == ctrl('l'):
                screen.redraw()
            elif inp.code in (term.KEY_LEFT, term.KEY_RIGHT):
                step = -1 if inp.code == term.KEY_LEFT else 1
                self.text_color = (self.text_color - 1 + step) % 15 + 1
                screen.text_style = Style(self.text_color, None)
                echo(screen.input_line())
            elif inp == u'\t':
                self.complete_nick()
                echo(screen.input_line())
            elif is_escape(term, inp):
                screen.input.text = u''
                self.update_prompt()
                echo(screen.input_line())
            else:
                line = screen.input.keystroke(term, inp)
                if isinstance(line, str):
                    if not self.command(line):
                        return False
                    self.update_prompt()
                    screen.flush()
                    echo(screen.top_row() + screen.input_line())
                elif line:
                    self.update_prompt()
                    echo(screen.input_line())
            if inp != u'\t':
                self.completion = None
            inp = term.inkey(0)
        return True

    def complete_nick(self):
        """ Complete the nick of the last word typed, of the room's users. """
        text = self.screen.input.text
        if self.completion is None:
            head, _, word = text.rpartition(u' ')
            if not word:
                return
            self.completion = (head, word, 0)
        head, word, idx = self.completion
        found = [user for user in self.users
                 if user.lower().startswith(word.lower())]
        if not found:
            return
        nick = found[idx % len(found)]
        self.completion = (head, word, idx + 1)
        tail = u' ' if head else u': '
        self.screen.input.text = (head + u' ' if head else u'') + nick + tail

    # received

    def handle_event(self, data):
        """ Handle event ``mrc`` of the engine. """
        kind, value = data[0], data[1]
        if kind == 'status':
            self.handle_status(value)
        elif kind == 'packet':
            try:
                self.handle_packet(value)
            except Exception:
                # pylint: disable=W0703
                #         Catching too general exception
                self.log.exception('mrc: handling {0!r}'.format(value))
        self.update_top()

    def handle_status(self, state):
        """ Handle the relay's state. """
        before, self.state = self.state, state
        if state == 'connected':
            if before is not None:
                self.system(u'Connected to MRC again.')
            if self.joined:
                self.announce()
        elif state == 'disconnected':
            self.system(u'The connection to MRC is lost; reconnecting.'
                        if before == 'connected' else
                        u'Connecting to MRC ...')
        elif state == 'disabled' and before is not None:
            self.error(u'MRC is not available.')

    def handle_packet(self, line):
        """ Handle packet ``line`` of the server. """
        packet = parse_packet(line)
        if packet is None:
            return
        if packet.from_user == u'SERVER' and self.server_command(packet.body):
            return
        if packet.to_room.lower() == CTCP_ROOM:
            self.inbound_ctcp(packet)
            return
        me = self.nick.upper()
        to_user = packet.to_user.upper()
        from_user = packet.from_user.upper()
        if packet.to_room and packet.to_room.upper() != self.room.upper():
            return
        if to_user and to_user != u'NOTME':
            if to_user != me or from_user == me:
                return
        if from_user == me and to_user == u'NOTME':
            return
        if from_user in (twit.upper() for twit in self.twits):
            return
        if from_user in RESERVED:
            self.show(packet.body)
            return
        if to_user == me:
            self.last_direct = packet.from_user
            self.show(packet.body, mark=True)
        else:
            self.show(packet.body, mark=(
                from_user != me and mentions(packet.body, self.nick)))
        if packet.from_user not in self.users:
            self.users.append(packet.from_user)

    def server_command(self, body):
        """ Handle ``body`` of the server, if it is a command of it. """
        keyword, _, value = body.partition(u':')
        if keyword == u'ROOMTOPIC':
            room, _, topic = value.partition(u':')
            if room.lower() == self.room.lower():
                self.topic = topic
        elif keyword == u'USERROOM':
            if value and value.lower() != self.room.lower():
                self.room, self.topic = room_name(value), u''
                self.send_server(u'USERLIST')
        elif keyword == u'USERLIST':
            self.users = [user for user in value.split(u',') if user]
        elif body == u'HELLO':
            self.send_server(u'IAMHERE')
        elif keyword == u'LATENCY':
            try:
                self.latency = min(999, int(value))
            except ValueError:
                pass
        elif keyword in (u'STATS', u'BANNER'):
            pass
        elif keyword == u'TERMINATE':
            self.error(value)
        elif keyword == u'USERNICK':
            self.nick = name_tag(value) or self.nick
            self.system(u'The server changed your nick to |15' + self.nick)
        else:
            return False
        return True

    def inbound_ctcp(self, packet):
        """ Handle CTCP request or reply ``packet``. """
        if packet.from_user.upper() == self.nick.upper():
            return
        words = packet.body.split(u' ')
        kind = words[0].upper()
        if kind == u'[CTCP]' and len(words) >= 4:
            requester, target, command = words[1], words[2], words[3].upper()
            self.show(u'|15* |14[CTCP-REQUEST] |15{0} |07on |15{1} |07from '
                      u'|10{2}'.format(command, target, packet.from_user))
            if target == u'*' or target.upper() in (
                    self.nick.upper(), u'#' + self.room.upper()):
                answer = {
                    u'VERSION': u'VERSION x/84 Multi Relay Chat',
                    u'TIME': u'TIME ' + time.strftime('%m/%d/%y %H:%M'),
                    u'PING': u' '.join([u'PING'] + words[4:]),
                    u'CLIENTINFO': u'CLIENTINFO VERSION TIME PING '
                                   u'CLIENTINFO',
                }.get(command)
                if answer:
                    self.send_ctcp(requester, u'[CTCP-REPLY]', answer)
        elif (kind == u'[CTCP-REPLY]' and len(words) >= 2 and
              packet.to_user.upper() == self.nick.upper()):
            self.show(u'|15* |14[CTCP-REPLY] |10{0} |15{1}'.format(
                words[1], u' '.join(words[2:])))

    # commands

    def command(self, line):
        """ Perform ``line`` typed; returns False to end the chat. """
        if not line.strip():
            return True
        if strip_pipes(line).startswith(u'/'):
            # the command is of its name without pipe codes, as typed of
            # the color of text; what follows it keeps them.
            name = strip_pipes(line)[1:].partition(u' ')[0].lower()
            rest = line.lstrip().partition(u' ')[2].strip()
            if name in (u'q', u'quit', u'exit'):
                self.leave(rest)
                return False
            method = getattr(self, 'cmd_' + {
                u'?': u'local_help', u'cls': u'clear', u't': u'msg',
                u'tell': u'msg', u'm': u'msg', u'pm': u'msg'}.get(
                    name, name), None)
            if method is not None and name in (u'?', u'colors', u'clear',
                                               u'cls', u'set', u'twit'):
                method(rest)
            elif self.state != 'connected' or not self.announced:
                self.error(u'Not connected to MRC.')
            elif method is not None:
                method(rest)
            elif name:
                # any other command is the server's.
                self.send_server(strip_pipes(line)[1:].strip())
            return True
        if self.state != 'connected' or not self.announced:
            self.error(u'Not connected to MRC.')
        else:
            self.send_room(u'{0}|{1:02d}{2}'.format(
                self.name_prompt(), self.text_color, ascii_text(line)))
        return True

    def cmd_local_help(self, _):
        """ /?: display commands of this client. """
        for usage, text in (
                (u'/join room', u'move to a room'),
                (u'/me action', u'describe what you are doing'),
                (u'/t user message', u'send a direct message (/msg, /tell)'),
                (u'/r message', u'reply to the last direct message'),
                (u'/b message', u'broadcast to every room'),
                (u'/topic text', u'change the topic of the room'),
                (u'/rooms', u'list the rooms'),
                (u'/bbses', u'list the BBSes connected'),
                (u'/ctcp target command', u'VERSION, TIME, PING, of a user, '
                                          u'#room or *'),
                (u'/twit add|del user', u'ignore a user, or /twit list'),
                (u'/set', u'list your settings, such as your nick color'),
                (u'/colors', u'display pipe codes of colors'),
                (u'/clear', u'clear the screen'),
                (u'/quit [message]', u'leave the chat')):
            self.show(u'|15* |15{0}|07{1}'.format(usage.ljust(22), text))
        self.system(u'Other commands are the server\'s, such as |15/help'
                    u'|11, |15/motd|11, |15/users |11and |15/chatters|11.')
        self.system(u'PgUp and PgDn scroll back, Up and Down recall lines '
                    u'typed, Left and Right change your color, Tab '
                    u'completes a nick.')

    def cmd_colors(self, _):
        """ /colors: display pipe codes of colors. """
        self.system(u'Color what you say by pipe codes:')
        self.show_literal(*[(Style(num, None), u'|{0:02d} '.format(num))
                            for num in range(1, 16)])
        self.show_literal(*[(Style(15, num - 16), u'|{0:02d}'.format(num))
                            for num in range(17, 24)])

    def cmd_clear(self, _):
        """ /clear: clear the screen. """
        self.screen.entries.clear()
        self.screen.layout()
        self.screen.redraw()

    def cmd_join(self, rest):
        """ /join room: move to a room. """
        room = room_name(rest.split(u' ')[0]) if rest else u''
        if not room:
            self.error(u'Usage: /join room')
        elif len(room) > MAX_ROOM:
            self.error(u'A room\'s name is at most {0} characters.'
                       .format(MAX_ROOM))
        elif room != self.room:
            self.join_room(room)

    def cmd_me(self, rest):
        """ /me action: describe what you are doing. """
        if rest:
            self.send_room(u'|15* |13{0} {1}'.format(
                self.nick, ascii_text(rest)))

    def cmd_msg(self, rest):
        """ /msg user message: send a direct message. """
        target, _, text = rest.partition(u' ')
        if not target or not text.strip():
            self.error(u'Usage: /t user message')
            return
        target = name_tag(target).upper()
        text = ascii_text(text.strip())
        self.send(target, u'', u'', u'|15* |08(|15{0}|08/|14DirectMsg|08) '
                  u'|07{1}'.format(self.nick, text))
        self.show(u'|15* |08(|14DirectMsg|08->|15{0}|08) |{1:02d}{2}'.format(
            target, self.text_color, text))

    def cmd_r(self, rest):
        """ /r message: reply to the last direct message. """
        if self.last_direct is None:
            self.error(u'No direct message has been received.')
        else:
            self.cmd_msg(u'{0} {1}'.format(self.last_direct, rest))

    def cmd_b(self, rest):
        """ /b message: broadcast to every room. """
        if rest:
            self.send(u'', u'', u'', u'|15* |08(|15{0}|08/|14Broadcast|08) '
                      u'|07{1}'.format(self.nick, ascii_text(rest)))

    def cmd_topic(self, rest):
        """ /topic text: change the room's topic. """
        if len(rest) > MAX_TOPIC:
            self.error(u'A topic is at most {0} characters.'
                       .format(MAX_TOPIC))
        elif rest:
            self.send_server(u'NEWTOPIC:{0}:{1}'.format(
                self.room, ascii_text(rest)))
        else:
            self.system(u'Topic: ' + (self.topic or u'(none)'))

    def cmd_rooms(self, _):
        """ /rooms: list the rooms. """
        self.send_server(u'LIST')

    def cmd_bbses(self, rest):
        """ /bbses: list the BBSes. """
        self.send_server(u'CONNECTED {0}'.format(rest).strip())

    def cmd_quote(self, rest):
        """ /quote command: send a command to the server. """
        if rest:
            self.send_server(rest)

    def cmd_ctcp(self, rest):
        """ /ctcp target command: send a CTCP request. """
        words = rest.split()
        if len(words) < 2:
            self.error(u'Usage: /ctcp user|#room|* VERSION|TIME|PING|'
                       u'CLIENTINFO')
            return
        target = words[0]
        to_user = (u'' if target == u'*' or target.startswith(u'#')
                   else name_tag(target))
        self.send_ctcp(to_user, u'[CTCP]', u' '.join(
            [target, words[1].upper()] + words[2:]))

    def cmd_twit(self, rest):
        """ /twit add|del|list|clear [user]: ignore users. """
        action, _, user = rest.partition(u' ')
        action, user = action.lower(), name_tag(user)
        if action == u'add' and user:
            if user.upper() in RESERVED:
                self.error(u'{0} may not be ignored.'.format(user))
                return
            self.twits = [twit for twit in self.twits
                          if twit.upper() != user.upper()] + [user]
            self.system(u'Ignoring |15' + user)
        elif action == u'del' and user:
            self.twits = [twit for twit in self.twits
                          if twit.upper() != user.upper()]
            self.system(u'No longer ignoring |15' + user)
        elif action == u'clear':
            self.twits = []
            self.system(u'Ignoring nobody.')
        elif action == u'list' or not action:
            self.system(u'Ignoring: |15' + (u' '.join(self.twits) or
                                            u'nobody'))
        else:
            self.error(u'Usage: /twit add|del user, or /twit list|clear')
            return
        self.save()

    def cmd_set(self, rest):
        """ /set [name value]: list or change this user's settings. """
        name, _, value = rest.partition(u' ')
        name, value = name.lower(), value.strip()
        if not name or name == u'list':
            for key in PREFERENCES:
                self.show_literal((Style(15, None), key.ljust(13)),
                                  (Style(7, None), self.prefs[key]),
                                  (Style(8, None), u'  ({0})'.format(
                                      PREFERENCE_HELP[key])))
            self.system(u'Change one by |15/set name value|11.')
            return
        if name not in PREFERENCES:
            self.error(u'No setting {0}; /set lists them.'.format(name))
            return
        value = ascii_text(value)
        visible = strip_pipes(value)
        if name == u'defaultroom':
            value = room_name(value)
            if not value or len(value) > MAX_ROOM:
                self.error(u'A room\'s name is 1 to {0} characters.'
                           .format(MAX_ROOM))
                return
        elif name == u'nickcolor':
            if not re.match(r'^\|(0[1-9]|1[0-5])$', value):
                self.error(u'A nick\'s color is a pipe code, of 01 to 15.')
                return
        elif name == u'ltbracket':
            if len(visible) != 1 or visible == u' ':
                self.error(u'The left bracket is one character.')
                return
        elif name == u'rtbracket':
            if not 1 <= len(visible) <= 8 or len(value) > 16 or (
                    u' ' in visible):
                self.error(u'The right bracket is 1 to 8 characters, not a '
                           u'space.')
                return
        elif not visible or len(value) > 80:
            self.error(u'A message is 1 to 80 characters.')
            return
        self.prefs[name] = value
        self.save()
        self.show_literal((Style(11, None), u'{0} is '.format(name)),
                          (Style(7, None), value))


def main():
    """ x/84 script launch point """
    term, session = getterminal(), getsession()
    session.activity = u'Chatting on MRC'
    screen = ChatScreen(term, session.encoding)
    chat = MRCChat(term, session, screen)
    screen.redraw()
    try:
        if chat.enter():
            chat.run()
        else:
            screen.flush()
            session.read_events(('input',), timeout=10)
    finally:
        chat.leave()
        screen.flush()
        echo(term.normal + term.clear)
