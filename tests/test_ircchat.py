""" Tests of the irc chat script of the default board. """
# std imports
import socket
import threading
import time

# 3rd-party
import pytest

pytest.importorskip('irc')
pyte = pytest.importorskip('pyte')

#: script of the chat, loaded once configured.
SCRIPT = 'ircchat'

ESC = u'\x1b'


@pytest.fixture
def ircchat(script):
    return script(SCRIPT)


def styles(runs):
    """ Return runs as (text, bold, fg, bg). """
    return [(text, style.bold, style.fg, style.bg) for style, text in runs]


def test_formatting_toggles(ircchat):
    # bold, italic, underline and reverse toggle; reset ends all.
    runs = ircchat.parse_formatting(
        u'\x02bold\x02 plain \x1ditalic\x1d \x02\x1fboth\x0f end')
    assert [(text, style.bold, style.italic, style.underline)
            for style, text in runs] == [
        (u'bold', True, False, False), (u' plain ', False, False, False),
        (u'italic', False, True, False), (u' ', False, False, False),
        (u'both', True, False, True), (u' end', False, False, False)]


def test_formatting_colors(ircchat):
    parse = ircchat.parse_formatting
    assert styles(parse(u'\x0304red\x03 plain')) == [
        (u'red', False, 4, None), (u' plain', False, None, None)]
    # two digits are always read, the background after a comma.
    assert styles(parse(u'\x0312345')) == [(u'345', False, 12, None)]
    assert styles(parse(u'\x033,12on blue')) == [(u'on blue', False, 3, 12)]
    # a comma not followed by a digit is text.
    assert styles(parse(u'\x034,text')) == [(u',text', False, 4, None)]
    # 99 is the default color; the background remains, of a foreground.
    assert styles(parse(u'\x034,2a\x0399b\x035c')) == [
        (u'a', False, 4, 2), (u'b', False, None, 2), (u'c', False, 5, 2)]
    # hex colors.
    (style, text), = parse(u'\x04FF8000orange')
    assert text == u'orange'
    assert ircchat.color_rgb(style.fg) == 0xff8000


def test_formatting_discards_controls(ircchat):
    # escape sequences, bells, and characters of direction are not
    # displayed; what remains of a sequence is text.  Tabs are spaces.
    runs = ircchat.parse_formatting(
        u'a\x1b[2J\x1b]0;title\x07b\x9bc\u202ed\te\x1e\x11')
    assert ircchat.plain_text(runs) == u'a[2J]0;titlebcd e'


def test_style_sequence(ircchat, session):
    from blessed import Terminal
    term = Terminal(kind='xterm-256color', force_styling=True)
    plain = ircchat.PLAIN
    # black, on the terminal's background, is dark gray: bright black.
    assert ircchat.style_sequence(term, plain._replace(fg=1)) == (
        term.normal + u'\x1b[90m')
    assert ircchat.style_sequence(term, plain._replace(fg=1, bg=0)) == (
        term.normal + term.black + term.on_white)
    # extended colors are of rgb, at 256 colors, or of the nearest mIRC
    # color, of fewer.
    assert ircchat.style_sequence(term, plain._replace(fg=52)) == (
        term.normal + term.color_rgb(255, 0, 0))
    ansi = Terminal(kind='ansi', force_styling=True)
    assert ircchat.style_sequence(ansi, plain._replace(fg=52)) == (
        ansi.normal + ansi.bold_red)
    assert ircchat.style_sequence(ansi, plain._replace(bg=12)) == (
        ansi.normal + ansi.on_blue)
    # bright colors of a terminal of 8 colors, such as xterm, which may
    # display bold of the normal color, are also of its bright colors.
    xterm = Terminal(kind='xterm', force_styling=True)
    assert ircchat.style_sequence(xterm, plain._replace(fg=14)) == (
        xterm.normal + xterm.bold + xterm.black + u'\x1b[90m')
    assert ircchat.style_sequence(xterm, plain._replace(fg=4)) == (
        xterm.normal + xterm.bold + xterm.red + u'\x1b[91m')


def test_wrap_runs(ircchat):
    # rows fit the width, of wide characters, too; styles continue onto
    # the next row, and spaces that end a row are removed.
    bold = ircchat.PLAIN._replace(bold=True)
    runs = [(ircchat.PLAIN, u'one two '), (bold, u'three four 日本語 five')]
    rows = ircchat.wrap_runs(runs, 10)
    assert [ircchat.plain_text(row) for row in rows] == [
        u'one two', u'three four', u'日本語', u'five']
    assert all(ircchat.text_width(ircchat.plain_text(row)) <= 10
               for row in rows)
    assert rows[2] == [(bold, u'日本語')]


def test_fold_text(ircchat):
    # a terminal of cp437 is sent what it displays, each of one cell: of
    # its letters, or without an accent, or '?'.
    assert ircchat.fold_text(u'café łódź 日本 e\u0301', 'cp437') == (
        u'café ?ódz ?? é')
    assert ircchat.fold_text(u'日本', 'utf8') == u'日本'


def test_nicks(ircchat):
    assert ircchat.sanitize_nick(u'Doc Brown', 16) == u'Doc_Brown'
    assert ircchat.sanitize_nick(u'José', 16) == u'Jose'
    assert ircchat.sanitize_nick(u'1985', 16) == u'_1985'
    assert ircchat.sanitize_nick(u'a' * 20, 16) == u'a' * 16
    candidates = ircchat.nick_candidates(u'marty', 16)
    assert candidates[:3] == [u'marty', u'marty_', u'marty1']
    long = ircchat.nick_candidates(u'emmettlathropbrown', 16)
    assert long[0] == u'emmettlathropbro'
    # of at most 9 characters, too, as of servers that allow no more.
    assert u'emmettlat' in long and u'emmettla_' in long


def test_split_message(ircchat):
    text = u' '.join([u'word'] * 50 + [u'ü' * 300])
    messages = ircchat.split_message(text, 100)
    assert all(len(message.encode('utf8')) <= 100 for message in messages)
    # split at spaces, but of a word longer than a message.
    assert u''.join(messages).replace(u' ', u'') == text.replace(u' ', u'')
    assert all(message.endswith(u'word') for message in messages[:2])
    assert ircchat.split_message(u'short', 100) == [u'short']
    # a long word is not split within a color code.
    assert ircchat.split_message(u'x' * 8 + u'\x0304,12red', 10) == [
        u'x' * 8, u'\x0304,12red']
    assert ircchat.split_message(u'x' * 6 + u'\x0304,12red', 10) == [
        u'x' * 6, u'\x0304,12red']


def test_mirc_encode(ircchat):
    assert ircchat.mirc_encode(u'|04red|nn |bbbold') == (
        u'\x0305red\x0f \x02bold')
    assert ircchat.mirc_encode(u'no codes') == u'no codes'


def test_ipv4_first(ircchat, monkeypatch):
    # IPv4 is tried first, as x/84 has always connected; IPv6 after.
    import socket
    v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, u'', (u'::1', 6697, 0, 0))
    v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, u'', (u'127.0.0.1', 6697))
    monkeypatch.setattr(ircchat.socket, 'getaddrinfo',
                        lambda *args, **kwargs: [v6, v4])
    assert ircchat.server_addresses((u'irc.example', 6697)) == [v4, v6]


def test_mentions(ircchat):
    assert ircchat.mentions(u'hey Marty, look', u'marty')
    assert not ircchat.mentions(u'martyr', u'marty')
    assert not ircchat.mentions(u'hello', None)


class FakeIRCd(threading.Thread):

    """
    An IRC server of one client, answering as servers do, and sending
    ``lines`` once the client joins its first channel.
    """

    def __init__(self, lines=(), taken=(), silent=False):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.lines, self.taken, self.silent = list(lines), taken, silent
        self.received = []
        self.done = threading.Event()

    def run(self):
        conn, _ = self.sock.accept()
        conn.settimeout(0.1)
        buf, nick, joined = b'', None, False
        stime = time.time()
        while time.time() - stime < 30 and not self.done.is_set():
            try:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
            except socket.timeout:
                continue
            except OSError:
                break
            while b'\r\n' in buf:
                line, buf = buf.split(b'\r\n', 1)
                line = line.decode('utf8')
                self.received.append(line)
                if self.silent:
                    continue
                command, _, rest = line.partition(u' ')

                def send(text):
                    conn.sendall(text.format(nick=nick, rest=rest)
                                 .encode('utf8') + b'\r\n')
                if command == u'NICK':
                    if rest in self.taken:
                        send(u':srv 433 * {rest} :Nickname is already in '
                             u'use')
                    elif nick is None:
                        nick = rest
                        send(u':srv 001 {nick} :Welcome, {nick}')
                    else:
                        send(u':' + nick + u'!u@h NICK {rest}')
                        nick = rest
                elif command == u'JOIN':
                    channel = rest.split()[0]
                    send(u':{nick}!u@h JOIN ' + channel)
                    send(u':srv 332 {nick} ' + channel + u' :the topic')
                    send(u':srv 353 {nick} = ' + channel +
                         u' :{nick} @alice +bob')
                    send(u':srv 366 {nick} ' + channel + u' :End of NAMES')
                    if not joined:
                        joined = True
                        for text in self.lines:
                            time.sleep(0.05)
                            send(text)
                elif command == u'PART':
                    send(u':{nick}!u@h PART ' + rest.split()[0])
                elif command == u'QUIT':
                    send(u'ERROR :Closing link')
                    conn.close()
                    return
        conn.close()


def run_chat(engine, ircchat, keys, until=None):
    """
    Run the chat, sending ``keys`` (a number of them pauses); returns the
    output written before ``until``, a key, was sent, and all output.
    """
    marks = {}

    def send():
        time.sleep(0.3)
        for key in keys:
            if isinstance(key, float):
                time.sleep(key)
                continue
            if key == until:
                marks['until'] = len(engine.output)
            engine.keys(key)
            time.sleep(0.1)

    sender = threading.Thread(target=send)
    sender.start()
    try:
        ircchat.main()
    finally:
        sender.join()
    time.sleep(0.1)
    return u''.join(engine.output[:marks.get('until')]), engine.text


def screen_of(output, rows=24, columns=80):
    screen = pyte.Screen(columns, rows)
    pyte.Stream(screen).feed(output)
    return screen


def configure(cfg, ircd, **options):
    cfg.set('irc', 'server', '127.0.0.1')
    cfg.set('irc', 'port', str(ircd.port))
    cfg.set('irc', 'ssl', 'no')
    cfg.set('irc', 'channel', '#bttf')
    for key, value in options.items():
        cfg.set('irc', key, value)


def test_chat(users, engine, script, session, cfg):
    # messages are displayed, of anything sent: a quit or kick of no
    # reason, an escape sequence; commands are sent; and the screen keeps
    # its shape: the input line at the bottom, nothing in the last column.
    ircd = FakeIRCd(lines=[
        u':alice!a@h PRIVMSG #bttf :hello everyone',
        u':carol!c@h PRIVMSG #bttf :evil \x1b[2J\x1b]0;pwned\x07 done',
        u':bob!b@h QUIT',
        u':alice!a@h KICK #bttf dave',
        u':bob!b@h PRIVMSG {nick} :a private word',
        u':alice!a@h PRIVMSG #bttf :\x01ACTION waves\x01',
        u':alice!a@h PRIVMSG {nick} :\x01VERSION\x01',
        u':alice!a@h PRIVMSG #bttf :' + u'long ' * 40 + u'end',
    ])
    ircd.start()
    configure(cfg, ircd)
    ircchat = script(SCRIPT)
    quit_key = u'/quit see you\r'
    keys = [1.5, u'hi there |04red\r', u'/msg bob hey\r', u'/me waves\r',
            u'/frob\r', 0.3, u'/join other\r', 0.5, quit_key]
    before, output = run_chat(engine, ircchat, keys, until=quit_key)
    ircd.done.set()

    received = ircd.received
    assert received[:2] == [u'NICK marty', u'USER marty 0 * :marty']
    for line in (u'JOIN #bttf', u'PRIVMSG #bttf :hi there \x0305red',
                 u'PRIVMSG bob :hey', u'PRIVMSG #bttf :\x01ACTION waves\x01',
                 u'NOTICE alice :\x01VERSION x/84 bbs ircchat\x01',
                 u'JOIN #other', u'PART #bttf', u'QUIT :see you'):
        assert line in received, line
    assert received.index(u'JOIN #other') < received.index(u'PART #bttf')
    # the escape sequence is not written, its rest displayed as text.
    assert u'evil [2J]0;pwned done' in before

    screen = screen_of(before)
    text = u'\n'.join(screen.display)
    for shown in (u'Topic of #bttf: the topic',
                  u'In #bttf (3): @alice +bob marty',
                  u'<alice> hello everyone', u'<carol> evil [2J]0;pwned done',
                  u'bob quit.', u'dave was kicked from #bttf by alice.',
                  u'*bob* a private word', u'* alice waves',
                  u'<marty> hi there red', u'-> *bob* hey', u'* marty waves',
                  u'Unknown command /frob', u'You joined #other.',
                  u'You left #bttf.'):
        assert shown in text, shown
    # the long message wraps, indented past the time.
    rows = [row for row in screen.display if u'long long' in row]
    assert len(rows) > 2 and rows[1].startswith(u' ' * 6 + u'long')
    assert screen.display[-1].startswith(u'[#other] ')
    assert all(row[79] == u' ' for row in screen.display)


def test_nick_in_use(users, engine, script, session, cfg):
    # another nick is tried, of a nick in use.
    ircd = FakeIRCd(taken=(u'marty', u'marty_'))
    ircd.start()
    configure(cfg, ircd)
    ircchat = script(SCRIPT)
    before, _ = run_chat(engine, ircchat, [1.0, u'/quit\r'], until=u'/quit\r')
    ircd.done.set()
    nicks = [line for line in ircd.received if line.startswith(u'NICK ')]
    assert nicks == [u'NICK marty', u'NICK marty_', u'NICK marty1']
    assert u'Connected to 127.0.0.1 as marty1' in u'\n'.join(
        screen_of(before).display)


def test_long_line(users, engine, script, session, cfg):
    # a line longer than the screen, and than a message, may be typed: it
    # scrolls as it is typed, and is sent as several messages.
    ircd = FakeIRCd()
    ircd.start()
    configure(cfg, ircd)
    ircchat = script(SCRIPT)
    words = u' '.join(u'word{0:03d}'.format(num) for num in range(100))
    before, _ = run_chat(engine, ircchat, [1.0, words, 0.5, u'\r', 1.0,
                                           u'/quit\r'], until=u'\r')
    ircd.done.set()
    sent = [line[len(u'PRIVMSG #bttf :'):] for line in ircd.received
            if line.startswith(u'PRIVMSG #bttf :')]
    assert len(words) == 799 and len(sent) == 2
    assert u' '.join(sent) == words
    row = screen_of(before).display[-1]
    assert row.startswith(u'[#bttf] ') and row.rstrip().endswith(u'word099')


def test_server_silent(users, engine, script, session, cfg):
    # a server that does not answer is given up, and return leaves.
    ircd = FakeIRCd(silent=True)
    ircd.start()
    configure(cfg, ircd, connect_timeout='1')
    ircchat = script(SCRIPT)
    stime = time.time()
    before, _ = run_chat(engine, ircchat, [3.0, u'\r'], until=u'\r')
    ircd.done.set()
    assert time.time() - stime < 6
    text = u'\n'.join(screen_of(before).display)
    assert u'The server did not answer.' in text
    assert u'Press Return to leave.' in text


def test_escape_cancels_connecting(users, engine, script, session, cfg):
    # escape cancels connecting, waiting on a server that does not answer
    # its ssl handshake.
    ircd = FakeIRCd(silent=True)
    ircd.start()
    configure(cfg, ircd, ssl='yes', connect_timeout='20')
    ircchat = script(SCRIPT)
    stime = time.time()
    run_chat(engine, ircchat, [0.5, ESC])
    ircd.done.set()
    assert time.time() - stime < 5


def test_cp437_terminal(users, engine, script, session, cfg):
    # characters not of cp437 are folded, each of one cell, so rows fit.
    session.session.env['encoding'] = 'cp437'
    if session.session.encoding != 'cp437':
        pytest.skip('session encoding is not of its env')
    ircd = FakeIRCd(lines=[u':alice!a@h PRIVMSG #bttf :łódź ' + u'日本 ' * 30])
    ircd.start()
    configure(cfg, ircd)
    ircchat = script(SCRIPT)
    before, _ = run_chat(engine, ircchat, [1.5, u'/quit\r'], until=u'/quit\r')
    ircd.done.set()
    screen = screen_of(before)
    assert any(u'<alice> ?ódz ?? ??' in row for row in screen.display)
    assert all(row[79] == u' ' for row in screen.display)
