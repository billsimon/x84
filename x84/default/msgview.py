"""
Full-screen message lists and reader, shared by the mail and board scripts.

A :class:`MessageScreen` lists messages, one per row, and displays the
selected message in a reader; subclasses provide the rows, titles and
commands.  Only ASCII is drawn, apart from message text, so that it
displays the same for utf8 and cp437 terminals, and only rows that
change are redrawn, for slow connections.

Also :class:`TagPicker`, to choose tags from a list.
"""
# std imports
import contextlib
import difflib
import re

# local
from x84.bbs import (
    getsession,
    getterminal,
    decode_pipe,
    LineEditor,
    list_users,
    find_user,
    get_ini,
    echo,
)
from x84.bbs.textbuffer import (
    RE_QUOTED,
    split_paragraphs,
    text_width,
    wrap_offsets,
)
from editor import ctrl, discard_csi, displayable, fit, is_escape

#: maximum length of user handles
username_max_length = get_ini(
    section='nua', key='max_user', getter='getint'
) or 10

#: maximum length of subjects
subject_max_length = get_ini(
    section='msg', key='max_subject', getter='getint'
) or 40

#: spaces between the name and subject columns of message lists.
GAP = 3

#: default colors, by name of terminal capability.
DEFAULT_COLORS = {
    'text': 'normal',
    'unread': 'bold',
    'bar': 'bold_white_on_blue',
    'select': 'reverse',
    'header': 'bold_magenta',
    'quote': 'bold_cyan',
    # columns of the message list, and of its unread messages.
    'flag': 'bold_red',
    'author': 'cyan',
    'author_unread': 'bold_cyan',
    'subject': 'normal',
    'subject_unread': 'bold_white',
    'date': 'green',
    'date_unread': 'bold_green',
}

#: color sequences, as decoded from pipe codes of message text.
RE_SGR = re.compile(r'\x1b\[[0-9;]*m')

KEY_REDRAW = ctrl('l')

#: keys of hints named by a word, such as 'Enter Read', and the PF keys of
#: a 3270 terminal that page (see x84.tn3270).
HINT_KEYS = {u'enter': u'\r', u'esc': u'\x1b', u'tab': u'\t', u'space': u' ',
             u'pf7': u'\x1b[5~', u'pf8': u'\x1b[6~'}


def hint_key(hint):
    """ Return key of ``hint``, such as 'Q Quit', or None. """
    word = hint.split(u' ', 1)[0]
    if word.lower() in HINT_KEYS:
        return HINT_KEYS[word.lower()]
    if len(word) == 2 and word[0] == u'^' and word[1].isalpha():
        return ctrl(word[1])
    if len(word) == 1 and word.isalpha():
        return word.lower()
    return None


def get_colors(term, section):
    """ Return dict of color functions, by name, from ``section``. """
    colors = {}
    for name, default in DEFAULT_COLORS.items():
        attr = get_ini(section=section,
                       key='color_{0}'.format(name)) or default
        if attr == 'normal':
            colors[name] = lambda text: text
        else:
            colors[name] = getattr(term, attr)
    return colors


def pad(text, width):
    """ Return ``text`` clipped or padded to ``width`` cells. """
    text = displayable(text, width)
    return text + u' ' * (width - text_width(text))


def prefix_subject(prefix, subject):
    """ Return ``subject`` prefixed by ``prefix``, such as ``'Re:'``. """
    subject = subject or u''
    if subject.lower().startswith(prefix.lower()):
        return subject
    return u'{0} {1}'.format(prefix, subject)[:subject_max_length]


def format_date(msg):
    """ Return date and time ``msg`` was sent, for display. """
    return msg.stime.strftime('%a %b %d %Y %H:%M')


def quote_header(msg):
    """ Return line introducing a quotation of given message, ``msg``. """
    return u'On {0}, {1} wrote:'.format(format_date(msg), msg.author)


class MessageScreen(object):

    """
    Full-screen list of messages, and reader of the selected one.

    Subclasses set :attr:`entries`, a list of messages, and implement
    :meth:`list_row`, :meth:`list_command` and :meth:`reader_command`,
    and may override the other ``list_`` and ``reader_`` methods.
    """

    # pylint: disable=R0902,R0904
    #         Too many instance attributes
    #         Too many public methods

    #: configuration section of colors.
    section = 'msgview'

    def __init__(self):
        self.session, self.term = getsession(), getterminal()
        self.colors = get_colors(self.term, self.section)
        self.entries = []
        self.index = self.top = 0
        self.notice = None
        self.screen = {}
        #: keys of hints displayed on bars, by row: (first, last, key) of
        #: their columns.
        self.hints = {}
        self.width = self.height = 0
        self.layout()

    # layout and display

    def layout(self):
        """ Size display to terminal. """
        self.width, self.height = self.term.width, self.term.height

    @property
    def list_height(self):
        """ Number of rows of the message list. """
        return max(1, self.height - 3)

    @property
    def text_height(self):
        """ Number of rows of the reader's text. """
        return max(1, self.height - 2)

    def clear(self):
        """ Clear screen, forgetting what is displayed. """
        self.screen = dict(
            (yloc, ('text', u'')) for yloc in range(self.height))
        echo(self.term.normal + self.term.clear)

    def draw_row(self, yloc, style, text):
        """
        Return sequence to display ``text`` of ``style`` at row ``yloc``.

        Only the part that differs from what is already displayed there
        is written, for slow connections.
        """
        text = displayable(text, self.width - 1)
        prior_style, prior = self.screen.get(yloc, (None, None))
        if (prior_style, prior) == (style, text):
            return u''
        self.screen[yloc] = (style, text)
        same = 0
        if prior_style == style:
            limit = min(len(prior), len(text))
            while same < limit and prior[same] == text[same]:
                same += 1
        result = self.term.move(yloc, text_width(text[:same]))
        if text[same:]:
            result += self.colors[style](text[same:])
        if prior is None or prior_style in ('raw', 'columns') or (
                text_width(prior) > text_width(text)):
            result += self.term.normal + self.term.clear_eol
        return result

    def draw_columns(self, yloc, columns):
        """
        Return sequence to display ``columns`` at row ``yloc``.

        ``columns`` is a sequence of (style, text) pairs, displayed side by
        side.  Only the columns from the first that differs from what is
        already displayed there are written.
        """
        fitted, width = [], self.width - 1
        for style, text in columns:
            text = displayable(text, width)
            width -= text_width(text)
            fitted.append((style, text))
        columns = tuple(fitted)
        prior_style, prior = self.screen.get(yloc, (None, None))
        if (prior_style, prior) == ('columns', columns):
            return u''
        self.screen[yloc] = ('columns', columns)
        same = 0
        if prior_style == 'columns':
            while (same < min(len(prior), len(columns)) and
                   prior[same] == columns[same]):
                same += 1
        xloc = sum(text_width(text) for _, text in columns[:same])
        result = self.term.move(yloc, xloc) + u''.join(
            self.colors[style](text) for style, text in columns[same:])
        shown = (sum(text_width(text) for _, text in prior)
                 if prior_style == 'columns' else
                 text_width(prior) if prior_style not in (None, 'raw')
                 else None)
        if shown is None or shown > xloc + sum(
                text_width(text) for _, text in columns[same:]):
            result += self.term.normal + self.term.clear_eol
        return result

    def draw_raw(self, yloc, text):
        """ Return sequence to display ``text`` of color sequences. """
        if self.screen.get(yloc) == ('raw', text):
            return u''
        self.screen[yloc] = ('raw', text)
        return u''.join((self.term.move(yloc, 0), self.term.normal, text,
                         self.term.normal, self.term.clear_eol))

    def draw_bar(self, yloc, left, right=u''):
        """
        Return sequence to display a title or status bar.

        ``right`` is text justified right, or a list of hints, such as
        ``['PF8 Down', 'Esc List']``.
        """
        items = [right] if isinstance(right, str) else right
        items = [item.strip() for item in items if item.strip()]
        right = u' '.join(items) + (u' ' if items else u'')
        self.hints[yloc] = []
        first = self.width - 1 - text_width(right)
        for item in items:
            if hint_key(item):
                self.hints[yloc].append(
                    (first, first + text_width(item), hint_key(item)))
            first += text_width(item) + 1
        return self.draw_row(yloc, 'bar', fit(left, right, self.width - 1))

    def page_hints(self):
        """ Return hints of the keys paging, at a 3270 terminal. """
        if self.session.kind != 'tn3270':
            return []
        return [u'PF7 Up', u'PF8 Down']

    def page(self, delta, count):
        """
        Scroll the list a page up (``delta`` of -1) or down (1).

        The selection moves as far, of ``count`` entries.
        """
        height = self.list_height
        self.index = max(0, min(count - 1, self.index + delta * height))
        self.top = max(0, min(count - height, self.top + delta * height))

    def draw_status(self, hints, right=u''):
        """ Return sequence to display status bar of key ``hints``. """
        if self.notice:
            return self.draw_bar(self.height - 1, u' ' + self.notice)
        left, keys = u'', []
        for hint in hints:
            if text_width(left + u' ' + hint + right) >= self.width - 1:
                break
            first = text_width(left) + 1
            left += u' ' + hint
            if hint_key(hint):
                keys.append((first, text_width(left), hint_key(hint)))
        result = self.draw_bar(self.height - 1, left, right)
        self.hints[self.height - 1] = keys
        return result

    def prompt(self, notice):
        """ Display ``notice`` on status bar and return next keystroke. """
        self.notice = notice
        echo(self.draw_status(()) + self.term.move(
            self.height - 1, min(self.width - 2, text_width(notice) + 1)))
        inp = self.term.inkey()
        self.notice = None
        return inp

    def confirm(self, question):
        """ Return whether ``question`` is answered yes. """
        return self.prompt(u'{0} (y/N)'.format(question)).lower() == u'y'

    # input

    @contextlib.contextmanager
    def mouse(self):
        """
        Context manager reporting clicks of the mouse, as keystrokes.

        Only for 3270 terminals, whose cursor, by ENTER, is the mouse (see
        x84.tn3270), for now: clicks are handled by :meth:`clicked`.
        """
        if self.session.kind != 'tn3270':
            yield
            return
        with self.term.mouse_enabled():
            yield

    def clicked(self, inp):
        """
        Return (row, column) of keystroke ``inp`` of a click, or None.

        Hints displayed on bars, such as 'Q Quit', are performed when
        clicked: their key is read next, and ``()`` is returned, as for
        other mouse events, which are ignored.  None is returned for other
        keystrokes.
        """
        if not (inp.name or u'').startswith('MOUSE_'):
            return None
        if inp.name != 'MOUSE_LEFT':
            return ()
        yloc, xloc = inp.mouse_yx
        for first, last, key in self.hints.get(yloc, ()):
            if first <= xloc < last:
                self.session.buffer_input(
                    key.encode(self.session.encoding), pushback=True)
                return ()
        return (yloc, xloc)

    def read_key(self):
        """
        Return next keystroke, or None after other events.

        The terminal may be resized, and new messages may arrive, while
        waiting for input.
        """
        inp = self.term.inkey(0)
        if inp:
            return inp
        event, data = self.session.read_events(
            ('refresh', 'newmsg', 'input'))
        if event == 'refresh':
            self.layout()
            self.clear()
        elif event == 'newmsg':
            self.session.flush_event('newmsg')
            self.new_message(data)
        elif event == 'input':
            self.session.buffer_input(data, pushback=True)
            return self.term.inkey(0) or None
        return None

    def new_message(self, idx):
        """ Called when message of index ``idx`` is saved by anyone. """

    def code(self, inp):
        """ Return keycode of keystroke ``inp``, or None. """
        if inp.is_sequence:
            if inp.name == 'CSI':
                discard_csi(self.term)
            return inp.code
        return None

    def prompt_line(self, yloc, label, content, width):
        """ Prompt for a line of text, returning None when canceled. """
        echo(self.term.move(yloc, 0) + self.term.normal + label +
             self.term.clear_eol)
        self.screen[yloc] = (None, None)
        return LineEditor(width, content,
                          colors={'highlight': self.colors['select']}).read()

    def prompt_recipient(self, yloc, handle=u'', allow_empty=False):
        """
        Prompt for handle of a user, returning None when canceled.

        When ``allow_empty``, an empty handle is returned as ``u''``.
        """
        while True:
            handle = self.prompt_line(yloc, u'     To: ', handle,
                                      username_max_length)
            if handle is None:
                return None
            handle = handle.strip()
            if not handle:
                return u'' if allow_empty else None
            match = find_user(handle)
            if match:
                return match
            by_lower = dict((user.lower(), user) for user in list_users())
            close = difflib.get_close_matches(handle.lower(), by_lower, n=1)
            if close and self.confirm(u'No user {0}; send to {1}?'.format(
                    handle, by_lower[close[0]])):
                return by_lower[close[0]]
            if not close:
                self.prompt(u'No user {0}. Press any key.'.format(handle))

    # message list

    @property
    def selected(self):
        """ Selected message, or None. """
        if 0 <= self.index < len(self.entries):
            return self.entries[self.index]
        return None

    def select(self, msg):
        """ Select message ``msg`` (or one of equal index), if listed. """
        for index, entry in enumerate(self.entries):
            if msg is not None and entry.idx == msg.idx:
                self.index = index
                return True
        return False

    def list_title(self):
        """ Return (left, right) text of title bar of the list. """
        return u' Messages', u''

    def list_header(self):
        """ Return text of column headings of the list. """
        return u''

    def list_row(self, msg):
        """
        Return text of list row of ``msg``, or its columns, a list of
        (style, text) pairs.
        """
        raise NotImplementedError

    def list_style(self, msg):
        """ Return display style of list row of ``msg``, of text. """
        return 'text'

    def list_hints(self):
        """ Return key hints of status bar of the list. """
        return [u'Enter Read', u'Q Quit']

    def list_empty(self):
        """ Return text displayed when there are no messages. """
        return u'  No messages.'

    def list_command(self, inp, msg):
        """
        Perform command of keystroke ``inp`` at the list.

        ``msg`` is the selected message, or None.  Returns True to quit.
        """
        return False

    def render_list(self):
        """ Display message list. """
        height = self.list_height
        if self.index < self.top or self.index >= self.top + height:
            self.top = max(0, self.index - height // 2)
        title, right = self.list_title()
        output = [self.draw_bar(0, title, self.page_hints() + [right]),
                  self.draw_row(1, 'header', self.list_header())]
        for yloc in range(height):
            idx = self.top + yloc
            text, style = u'', 'text'
            if idx < len(self.entries):
                msg = self.entries[idx]
                text = self.list_row(msg)
                if idx == self.index:
                    style = 'select'
                    if not isinstance(text, str):
                        text = u''.join(column for _, column in text)
                elif not isinstance(text, str):
                    output.append(self.draw_columns(2 + yloc, text))
                    continue
                else:
                    style = self.list_style(msg)
            elif idx == 0:
                text = self.list_empty()
            output.append(self.draw_row(2 + yloc, style, text))
        right = u'{0}/{1} '.format(
            self.index + 1, len(self.entries)) if self.entries else u''
        output.append(self.draw_status(self.list_hints(), right))
        output.append(self.term.move(2 + self.index - self.top, 0))
        echo(u''.join(output))

    def run(self):
        """ Display message list until quit. """
        with self.mouse():
            return self.run_list()

    def run_list(self):
        """ Display message list until quit, see :meth:`run`. """
        term = self.term
        self.clear()
        while True:
            self.render_list()
            inp = self.read_key()
            if inp is None:
                continue
            self.notice = None
            click = self.clicked(inp)
            if click is not None:
                # a message clicked is read.
                if click and 2 <= click[0] < 2 + self.list_height and (
                        self.top + click[0] - 2 < len(self.entries)):
                    self.index = self.top + click[0] - 2
                    self.reader()
                continue
            code = self.code(inp)
            last = len(self.entries) - 1
            msg = self.selected
            if code == term.KEY_UP:
                self.index = max(0, self.index - 1)
            elif code == term.KEY_DOWN:
                self.index = max(0, min(last, self.index + 1))
            elif code == term.KEY_PGUP:
                self.page(-1, len(self.entries))
            elif code == term.KEY_PGDOWN:
                self.page(1, len(self.entries))
            elif code == term.KEY_HOME:
                self.index = 0
            elif code == term.KEY_END:
                self.index = max(0, last)
            elif code in (term.KEY_ENTER, term.KEY_RIGHT) and msg:
                self.reader()
            elif inp == KEY_REDRAW:
                self.clear()
            elif self.list_command(inp, msg):
                return
            elif is_escape(term, inp) or inp.lower() == u'q':
                return

    # reader

    def reader_header(self, msg):
        """ Return list of (label, value) of header lines of ``msg``. """
        return [(u'From', msg.author),
                (u'To', msg.recipient or u'All'),
                (u'Date', format_date(msg)),
                (u'Subject', msg.subject or u'(no subject)')]

    def reader_title(self, msg):
        """ Return (left, right) text of title bar of the reader. """
        return (u' Message {0}/{1}'.format(self.index + 1, len(self.entries)),
                u'Esc List ')

    def reader_hints(self, msg):
        """ Return key hints of status bar of the reader. """
        return [u'N Next', u'P Prev', u'Q List']

    def reader_open(self, msg):
        """ Called when ``msg`` is displayed, such as to mark it read. """

    def reader_command(self, inp, msg):
        """
        Perform command of keystroke ``inp`` while reading ``msg``.

        Returns True to return to the list.
        """
        return False

    def next_index(self, delta):
        """ Return index of next (or previous) message to read, or None. """
        index = self.index + delta
        if 0 <= index < len(self.entries):
            return index
        return None

    def reader_rows(self, msg):
        """ Return list of (style, text) rows displaying ``msg``. """
        term, width = self.term, self.width - 1
        header = self.reader_header(msg)
        label_width = max(len(label) for label, _ in header)
        rows = [('header', u'{0}: {1}'.format(label.rjust(label_width), value))
                for label, value in header]
        rows.append(('text', u''))
        for para in split_paragraphs(msg.body or u''):
            style = 'quote' if RE_QUOTED.match(para) else 'text'
            decoded = decode_pipe(para) if u'|' in para else para
            if decoded == para:
                offsets = wrap_offsets(para, width) + (len(para),)
                rows.extend((style, para[begin:end]) for begin, end in
                            zip(offsets, offsets[1:]))
                continue
            # color codes of pipe text: each row begins with the color
            # sequences of rows before it, as wrapped from one line.
            carry = u''
            for row in term.wrap(decoded, width, drop_whitespace=False) or [
                    u'']:
                rows.append(('raw', carry + row))
                carry += u''.join(RE_SGR.findall(row))
        return rows

    def reader(self):
        """ Display selected message until returning to the list. """
        term = self.term
        msg, rows, top, rows_width = None, [], 0, None
        self.clear()
        while True:
            if self.selected is None:
                return
            if msg is None or msg.idx != self.selected.idx:
                msg, top = self.selected, 0
                self.reader_open(msg)
                rows, rows_width = self.reader_rows(msg), self.width
            elif rows_width != self.width:
                rows, rows_width = self.reader_rows(msg), self.width
            height = self.text_height
            last_top = max(0, len(rows) - height)
            top = min(top, last_top)
            title, right = self.reader_title(msg)
            output = [self.draw_bar(0, title, self.page_hints() + [right])]
            for yloc in range(height):
                idx = top + yloc
                style, text = rows[idx] if idx < len(rows) else ('text', u'')
                output.append(self.draw_raw(1 + yloc, text) if style == 'raw'
                              else self.draw_row(1 + yloc, style, text))
            output.append(self.draw_status(
                self.reader_hints(msg),
                u'{0}%'.format(100 * min(len(rows), top + height) //
                               max(1, len(rows))).rjust(5) + u' '))
            output.append(term.move(self.height - 1, 0))
            echo(u''.join(output))

            inp = self.read_key()
            if inp is None or self.clicked(inp) is not None:
                continue
            self.notice = None
            code = self.code(inp)
            if code == term.KEY_UP:
                top = max(0, top - 1)
            elif code == term.KEY_DOWN:
                top = min(last_top, top + 1)
            elif code == term.KEY_PGUP:
                top = max(0, top - height)
            elif code == term.KEY_HOME:
                top = 0
            elif code == term.KEY_END:
                top = last_top
            elif code in (term.KEY_PGDOWN, term.KEY_ENTER) or inp == u' ':
                if top < last_top:
                    top = min(last_top, top + height)
                elif not self.step(1):
                    return
            elif code == term.KEY_RIGHT or inp.lower() == u'n':
                if not self.step(1):
                    echo(u'\a')
            elif code == term.KEY_LEFT or inp.lower() == u'p':
                if not self.step(-1):
                    echo(u'\a')
            elif inp == KEY_REDRAW:
                self.clear()
            elif self.reader_command(inp, msg):
                return
            elif is_escape(term, inp) or inp.lower() == u'q':
                return

    def step(self, delta):
        """ Select next (or previous) message, return whether moved. """
        index = self.next_index(delta)
        if index is None:
            return False
        self.index = index
        return True


class TagPicker(MessageScreen):

    """
    Choose tags from a list, by Space, returning them by Enter.

    Each tag is listed with its number of messages.  Tags (or patterns,
    such as ``'lang-*'``) chosen but not listed are added, and new ones
    may be typed when ``allow_new``.
    """

    def __init__(self, title, tags, chosen=(), allow_new=True,
                 validate=None):
        """
        Class initializer.

        :param str title: displayed in the title bar.
        :param dict tags: number of messages, by tag.
        :param chosen: tags chosen at first.
        :param bool allow_new: whether tags may be typed by ``A``.
        :param validate: function returning an error message for a typed
            tag, or None when it is accepted.
        """
        super().__init__()
        self.title = title
        self.counts = dict(tags)
        self.allow_new = allow_new
        self.validate = validate
        self.chosen = set(chosen)
        names = sorted(self.counts, key=lambda tag: (-self.counts[tag], tag))
        extra = sorted(set(self.chosen) - set(names))
        self.entries = extra + names

    @property
    def list_height(self):
        return max(1, self.height - 2)

    def describe(self, tag):
        """ Return description of ``tag``. """
        if tag == u'*':
            return u'all messages'
        if tag in self.counts:
            count = self.counts[tag]
            return u'{0} message{1}'.format(count, u'' if count == 1 else u's')
        if any(ucs in tag for ucs in u'*?['):
            return u'pattern'
        return u'new'

    def render(self):
        """ Display tag list. """
        height = self.list_height
        if self.index < self.top or self.index >= self.top + height:
            self.top = max(0, self.index - height // 2)
        output = [self.draw_bar(0, u' ' + self.title, self.page_hints() + [
            u'{0} chosen '.format(len(self.chosen))])]
        width = max([12] + [text_width(tag) + 1 for tag in self.entries])
        width = min(width, max(12, self.width - 24))
        for yloc in range(height):
            idx = self.top + yloc
            text, style = u'', 'text'
            if idx < len(self.entries):
                tag = self.entries[idx]
                text = u' [{0}] {1} {2}'.format(
                    u'x' if tag in self.chosen else u' ', pad(tag, width),
                    self.describe(tag))
                style = ('select' if idx == self.index else
                         'unread' if tag in self.chosen else 'text')
            elif idx == 0:
                text = u'  No tags.'
            output.append(self.draw_row(1 + yloc, style, text))
        hints = [u'Space Choose', u'Enter Accept']
        if self.allow_new:
            hints.append(u'A Add')
        hints.append(u'Esc Cancel')
        output.append(self.draw_status(hints))
        output.append(self.term.move(1 + self.index - self.top, 1))
        echo(u''.join(output))

    def add(self):
        """ Prompt for a tag to add. """
        tag = self.prompt_line(self.height - 1, u' Add tag: ', u'',
                               subject_max_length)
        self.screen[self.height - 1] = (None, None)
        tag = (tag or u'').strip()
        if not tag:
            return
        error = self.validate(tag) if self.validate else None
        if error:
            self.notice = error
            return
        if tag not in self.entries:
            self.entries.insert(0, tag)
        self.chosen.add(tag)
        self.index = self.entries.index(tag)

    def run(self):
        """ Return sorted list of tags chosen, or None when canceled. """
        with self.mouse():
            return self.run_picker()

    def run_picker(self):
        """ Return tags chosen, see :meth:`run`. """
        term = self.term
        self.clear()
        while True:
            self.render()
            inp = self.read_key()
            if inp is None:
                continue
            self.notice = None
            click = self.clicked(inp)
            if click is not None:
                # a tag clicked is chosen, or no longer.
                if click and 1 <= click[0] <= self.list_height and (
                        self.top + click[0] - 1 < len(self.entries)):
                    self.index = self.top + click[0] - 1
                    self.chosen.symmetric_difference_update(
                        [self.entries[self.index]])
                continue
            code = self.code(inp)
            last = len(self.entries) - 1
            if code == term.KEY_UP:
                self.index = max(0, self.index - 1)
            elif code == term.KEY_DOWN:
                self.index = max(0, min(last, self.index + 1))
            elif code == term.KEY_PGUP:
                self.page(-1, len(self.entries))
            elif code == term.KEY_PGDOWN:
                self.page(1, len(self.entries))
            elif code == term.KEY_HOME:
                self.index = 0
            elif code == term.KEY_END:
                self.index = max(0, last)
            elif inp == u' ' and self.entries:
                self.chosen.symmetric_difference_update(
                    [self.entries[self.index]])
                self.index = max(0, min(last, self.index + 1))
            elif code == term.KEY_ENTER:
                return sorted(self.chosen)
            elif inp == KEY_REDRAW:
                self.clear()
            elif is_escape(term, inp):
                return None
            elif inp.lower() == u'a' and self.allow_new:
                self.add()
