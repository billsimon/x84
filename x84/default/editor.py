"""
Full-screen editor for x/84.

Text is typed directly, wrapping at word boundaries, and the cursor
moves freely with the arrow keys.  Commands are control keys, listed on
the status bar, or chosen from a menu by pressing Escape:

- ``^Z`` save, ``^A`` abort, ``^G`` help.
- ``^Q`` opens a window to choose lines of a replied message to quote.
- ``^Y`` cuts the current line, ``^U`` pastes cut lines.
- ``^W`` deletes the word left of the cursor, ``^D`` the character under
  it, and ``^V`` (or Insert) toggles insert and overwrite mode.

At a 3270 terminal (see x84.tn3270), each row of text is an input field,
typed over at the terminal and sent by ENTER or a PF key, which may add a
line (ENTER, at the end of one), page (PF7, PF8), display help (PF1), or
the menu (PF3, PF12).  A command may be typed by its letter, as a command
(after ``Command ===>``).

The editor draws only ASCII, so that it displays the same for utf8 and
cp437 terminals.  Its colors may be changed in section ``[editor]`` of
the configuration: ``color_text``, ``color_quote``, ``color_bar``,
``color_mark`` and ``color_select``.
"""
# std imports
import os
import time

# local
from x84.bbs import getsession, getterminal, get_ini, decode_pipe, echo
from x84.bbs import TextBuffer
from x84.bbs.textbuffer import RE_QUOTED, char_width, quote_lines, text_width

here = os.path.dirname(__file__)

#: seconds between saving drafts, when a draft key is given.
AUTOSAVE_SECONDS = 15

#: maximum number of characters of a document.
max_length = get_ini(
    section='editor', key='max_length', getter='getint'
) or 65536


def ctrl(ucs):
    """ Return control character of letter ``ucs``, such as ``^Z``. """
    return chr(ord(ucs.upper()) & 0x1f)


KEY_SAVE = ctrl('z')
KEY_ABORT = ctrl('a')
KEY_QUOTE = ctrl('q')
KEY_HELP = ctrl('g')
KEY_CUT = ctrl('y')
KEY_PASTE = ctrl('u')
KEY_DELWORD = ctrl('w')
KEY_DELETE = ctrl('d')
KEY_INSERT = ctrl('v')
KEY_REDRAW = ctrl('l')

#: default colors, by name of terminal capability.
DEFAULT_COLORS = {
    'text': 'normal',
    'quote': 'bold_cyan',
    'bar': 'bold_white_on_blue',
    'mark': 'black_on_cyan',
    'select': 'reverse',
}


def get_colors(term):
    """ Return dict of color functions, by name, from configuration. """
    colors = {}
    for name, default in DEFAULT_COLORS.items():
        attr = get_ini(section='editor',
                       key='color_{0}'.format(name)) or default
        if attr == 'normal':
            colors[name] = lambda text: text
        else:
            colors[name] = getattr(term, attr)
    return colors


def displayable(text, width):
    """
    Return ``text`` clipped to ``width`` cells, safe to display.

    Control characters are displayed as ``'?'``.
    """
    result, cells = [], 0
    for ucs in text:
        cells += char_width(ucs)
        if cells > width:
            break
        result.append(u'?' if ucs < u' ' or ucs == u'\x7f' else ucs)
    return u''.join(result)


def fit(left, right, width):
    """ Return ``left`` and ``right`` justified to ``width`` cells. """
    space = width - text_width(left) - text_width(right)
    if space < 1:
        return displayable(left, width).ljust(width)
    return left + u' ' * space + right


def is_escape(term, inp):
    """
    Whether keystroke ``inp`` is the escape key.

    Escape followed quickly by another key, such as on slow connections,
    is received as one alt- key (or ctrl-alt- key, of a control key), also
    matched: its second key is ``inp[1:]``, a string, which may be the
    escape key, too.
    """
    if not hasattr(inp, 'is_sequence'):
        return inp == u'\x1b'
    return (inp.is_sequence and inp.code == term.KEY_ESCAPE or
            (inp.name or u'').startswith(('KEY_ALT_', 'KEY_CTRL_ALT_')) and
            inp[:1] == u'\x1b')


def declare_fields(top=None, bottom=None):
    """
    Return sequence making rows ``top`` to ``bottom`` input fields.

    Only for a 3270 terminal, see x84.tn3270; without arguments, no rows
    are input fields.
    """
    if top is None:
        return u'\x1b]3270;fields\x07'
    return u'\x1b]3270;fields;{0};{1}\x07'.format(top, bottom)


def discard_csi(term):
    """ Discard remainder of an unrecognized CSI sequence. """
    while True:
        inp = term.inkey(0.1)
        if not inp or u'@' <= inp[-1] <= u'~':
            return


class Editor(object):

    """ Full-screen editor of a :class:`TextBuffer`. """

    # pylint: disable=R0902
    #         Too many instance attributes

    def __init__(self, text=u'', title=None, quote=None, quote_header=None,
                 draft_key=None):
        self.session, self.term = getsession(), getterminal()
        self.colors = get_colors(self.term)
        self.buf = TextBuffer(text, max_length=max_length)
        self.title = title or u'Editor'
        self.quote, self.quote_header = quote, quote_header
        self.quote_inserted = False
        self.draft_key = draft_key
        self.draft_saved = (time.time(), self.buf.text)
        self.notice = None
        self.last_key = None
        self.top = 0
        self.screen = {}
        #: whether rows of text are input fields of a 3270 terminal, and
        #: the rows declared so.
        self.fields = self.session.kind == 'tn3270'
        self.declared = None
        self.width = self.height = 0
        self.layout()

    # layout and display

    @property
    def text_height(self):
        """ Number of rows of text area, between title and status bar. """
        return max(1, self.height - 2)

    def layout(self):
        """ Size editor to terminal. """
        self.width, self.height = self.term.width, self.term.height
        # the last column remains for the cursor at the end of a full row.
        self.buf.width = max(10, self.width - 1)

    def draw_row(self, yloc, style, text):
        """
        Return sequence to display ``text`` of ``style`` at row ``yloc``.

        Only the part that differs from what is already displayed there
        is written, so that typing is quick even on slow connections.
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
        if prior is None or text_width(prior) > text_width(text):
            result += self.term.normal + self.term.clear_eol
        return result

    def draw_bar(self, yloc, left, right=u''):
        """ Return sequence to display a title or status bar. """
        return self.draw_row(yloc, 'bar', fit(left, right, self.width - 1))

    def status_text(self):
        """ Return (left, right) text of status bar. """
        if self.notice:
            return u' ' + self.notice, u''
        col = self.buf.cursor_rowcol()[1]
        if self.fields:
            hints = [u'Enter New line', u'PF1 Help', u'PF3 Menu',
                     u'PF7/PF8 Page']
            right = u'Ln {0}/{1} Col {2} '.format(
                self.buf.para + 1, len(self.buf.paragraphs), col + 1)
        else:
            hints = [u'^Z Save', u'^A Abort']
            if self.quote:
                hints.append(u'^Q Quote')
            hints.append(u'^G Help')
            right = u'Ln {0}/{1} Col {2} {3} '.format(
                self.buf.para + 1, len(self.buf.paragraphs), col + 1,
                u'OVR' if self.buf.overwrite else u'INS')
        left = u''
        for hint in hints:
            item = u' ' + hint
            if text_width(left + item + right) >= self.width - 1:
                break
            left += item
        return left, right

    def render(self, full=False):
        """ Display changes to the editor since last call. """
        term = self.term
        output = []
        if full:
            output.append(term.normal + term.clear)
            self.screen = dict(
                (yloc, ('text', u'')) for yloc in range(self.height))
        rows = self.buf.rows()
        row, col = self.buf.cursor_rowcol()
        height = self.text_height
        if row < self.top or row >= self.top + height:
            # scroll by half a page, rather than every row.
            self.top = max(0, row - height // 2)
        for yloc in range(height):
            idx = self.top + yloc
            text, style = u'', 'text'
            if idx < len(rows):
                text = self.buf.row_text(rows[idx])
                if RE_QUOTED.match(self.buf.paragraphs[rows[idx][0]]):
                    style = 'quote'
            output.append(self.draw_row(1 + yloc, style, text))
        output.append(self.draw_bar(0, u' ' + self.title, u'{0} Menu '.format(
            u'PF3' if self.fields else u'Esc')))
        output.append(self.draw_bar(self.height - 1, *self.status_text()))
        if self.fields and self.declared != (1, height):
            self.declared = (1, height)
            output.append(declare_fields(1, height))
        output.append(term.move(1 + row - self.top, min(col, self.width - 1)))
        echo(u''.join(output))

    def leave_fields(self):
        """ Make rows of text no longer input fields of a 3270 terminal. """
        if self.declared:
            self.declared = None
            echo(declare_fields())

    def prompt(self, notice):
        """ Display ``notice`` on status bar and return next keystroke. """
        self.notice = notice
        self.render()
        # at a 3270, the cursor is then of the input row, see x84.tn3270.
        echo(self.term.move(self.height - 1,
                            min(self.width - 2, text_width(notice) + 1)))
        if self.fields:
            return self.prompt_fields()
        inp = self.term.inkey()
        self.notice = None
        return inp

    def prompt_fields(self):
        """
        Return the answer to a prompt, typed at a 3270 terminal.

        That is the first letter typed as a command, Return when none, or
        Escape by PF3.  What was typed in the text meanwhile is kept.
        """
        while True:
            event, data = self.session.read_events(('refresh', 'tn3270'))
            if event == 'refresh':
                self.layout()
                self.render(full=True)
                continue
            self.apply_fields(data)
            self.notice = None
            if data['aid'] in ('pf3', 'pf12'):
                return u'\x1b'
            return data['text'].strip()[:1] or u'\r'

    def apply_fields(self, data):
        """
        Apply what was typed in the rows of text, at a 3270 terminal.

        ``data`` is the event of an attention key, of x84.tn3270: rows of
        text typed over, by row of the screen, and the cursor.
        """
        height = self.text_height
        changes = dict((self.top + yloc - 1, text)
                       for yloc, text in data['fields'].items()
                       if 1 <= yloc <= height)
        if changes and not self.buf.replace_rows(changes):
            echo(u'\a')
        if data['cursor'] and 1 <= data['cursor'][0] <= height:
            self.buf.locate(self.top + data['cursor'][0] - 1,
                            data['cursor'][1])

    def handle_fields(self, data):
        """
        Process an attention key of a 3270 terminal.

        Returns ``(True, value)`` when the editor exits with ``value``.
        """
        buf = self.buf
        self.apply_fields(data)
        command = data['text'].strip()
        if command:
            if command[:1].upper() not in u'SAUQH':
                self.notice = u'Commands: Save, Abort, Undo, Quote, Help'
                return None
            return self.menu(command[:1])
        if data['aid'] == 'enter':
            # a line is added when typing has reached the end of one.
            if data['cursor'] and buf.pos == len(buf.paragraphs[buf.para]):
                if not buf.newline():
                    echo(u'\a')
        elif data['aid'] in ('pf3', 'pf12'):
            # END and CANCEL, as ISPF names them.
            return self.menu()
        elif data['aid'] == 'pf1':
            return self.command(KEY_HELP)
        elif data['aid'] == 'pf7':
            buf.up(self.text_height - 1)
        elif data['aid'] == 'pf8':
            buf.down(self.text_height - 1)
        return None

    # drafts

    def autosave(self, force=False):
        """ Save draft, when due. """
        if not self.draft_key:
            return
        stime, text = self.draft_saved
        if text != self.buf.text and (
                force or time.time() - stime >= AUTOSAVE_SECONDS):
            self.session.user[self.draft_key] = self.buf.text
            self.draft_saved = (time.time(), self.buf.text)

    def autosave_timeout(self):
        """ Seconds until the draft should be saved, or None. """
        stime, text = self.draft_saved
        if not self.draft_key or text == self.buf.text:
            return None
        return max(0.1, stime + AUTOSAVE_SECONDS - time.time())

    def discard_draft(self):
        """ Remove saved draft. """
        if self.draft_key and self.session.user.get(self.draft_key):
            del self.session.user[self.draft_key]

    # sub-screens

    def show_help(self):
        """ Display help text, scrolled by arrow keys. """
        term = self.term
        with open(os.path.join(here, 'editor.txt')) as fin:
            lines = [decode_pipe(line) for line in fin.read().splitlines()]
        height = self.text_height
        last_top = max(0, len(lines) - height)
        top = 0
        while True:
            output = [
                self.draw_bar(0, u' Help'),
                self.draw_bar(self.height - 1,
                              u' Up/Down/PgUp/PgDn scroll, '
                              u'any other key returns',
                              u'{0}/{1} '.format(top + 1, len(lines)))]
            # lines contain color sequences of decoded pipe codes, they
            # are drawn whole rather than by draw_row().
            for yloc in range(height):
                idx = top + yloc
                self.screen[1 + yloc] = (None, None)
                output.append(term.move(1 + yloc, 0) + term.normal +
                              term.clear_eol)
                if idx < len(lines):
                    output.append(lines[idx] + term.normal)
            echo(u''.join(output))
            inp = term.inkey()
            code = inp.code if inp.is_sequence else None
            if code == term.KEY_DOWN:
                top = min(last_top, top + 1)
            elif code == term.KEY_UP:
                top = max(0, top - 1)
            elif code == term.KEY_PGDOWN:
                top = min(last_top, top + height)
            elif code == term.KEY_PGUP:
                top = max(0, top - height)
            else:
                return

    def quote_window(self):
        """ Choose lines of the replied message to insert as quotation. """
        term = self.term
        # lines are displayed following a mark column of 2 cells.
        lines = quote_lines(self.quote, self.buf.width - 2)
        height = self.text_height
        marks, index, top = set(), 0, 0
        while True:
            if index < top or index >= top + height:
                top = max(0, index - height // 2)
            output = [self.draw_bar(
                0, u' Quote: Space marks, A all, Enter inserts, '
                u'Esc cancels')]
            for yloc in range(height):
                idx = top + yloc
                text, style = u'', 'text'
                if idx < len(lines):
                    text = (u'+ ' if idx in marks else u'  ') + lines[idx]
                    style = ('select' if idx == index else
                             'mark' if idx in marks else 'quote')
                output.append(self.draw_row(1 + yloc, style, text))
            output.append(self.draw_bar(
                self.height - 1, u' {0} marked'.format(len(marks)),
                u'Line {0}/{1} '.format(index + 1, len(lines))))
            output.append(term.move(1 + index - top, 0))
            echo(u''.join(output))

            inp = term.inkey()
            code = inp.code if inp.is_sequence else None
            if code == term.KEY_UP:
                index = max(0, index - 1)
            elif code == term.KEY_DOWN:
                index = min(len(lines) - 1, index + 1)
            elif code == term.KEY_PGUP:
                index = max(0, index - height)
            elif code == term.KEY_PGDOWN:
                index = min(len(lines) - 1, index + height)
            elif code == term.KEY_HOME:
                index = 0
            elif code == term.KEY_END:
                index = len(lines) - 1
            elif inp == u' ':
                marks.symmetric_difference_update([index])
                index = min(len(lines) - 1, index + 1)
            elif inp.lower() == u'a':
                if len(marks) == len(lines):
                    marks.clear()
                else:
                    marks.update(range(len(lines)))
            elif code == term.KEY_ENTER:
                break
            elif is_escape(term, inp):
                return

        chosen = [lines[idx] for idx in sorted(marks or [index])]
        if self.quote_header and not self.quote_inserted:
            chosen.insert(0, self.quote_header)
        if self.buf.insert_paragraphs(chosen):
            self.quote_inserted = True
        else:
            echo(u'\a')

    def menu(self, inp=None):
        """
        Command menu of escape key, returns result of command.

        The menu is displayed and a choice prompted, unless already given
        as keystroke ``inp``.
        """
        choices = [(u'S', u'ave', KEY_SAVE), (u'A', u'bort', KEY_ABORT),
                   (u'U', u'ndo', None)]
        if self.quote:
            choices.append((u'Q', u'uote', KEY_QUOTE))
        choices.append((u'H', u'elp', KEY_HELP))
        if not inp:
            inp = self.prompt(u'Menu: {0}  ({1} returns)'.format(u'  '.join(
                u'{0}){1}'.format(key, rest) for key, rest, _ in choices),
                u'PF3' if self.fields else u'Esc'))
        if len(inp) == 1 and inp < u' ' and not is_escape(self.term, inp):
            # control keys of commands, such as ^Z, work here as well.
            return self.command(str(inp))
        for key, _, command in choices:
            if inp.upper() == key:
                if command is not None:
                    return self.command(command)
                if not self.buf.undo():
                    echo(u'\a')
        return None

    # input

    def command(self, inp):
        """
        Perform command of control key ``inp``.

        Returns ``(True, value)`` when the editor exits with ``value``.
        """
        buf = self.buf
        if inp == KEY_SAVE:
            return True, buf.text
        elif inp == KEY_ABORT:
            if buf.modified and not buf.is_empty:
                inp = self.prompt(u'Abort, discarding your text? (y/N)')
                if inp.lower() != u'y':
                    return None
            return True, False
        elif inp == KEY_HELP:
            self.leave_fields()
            self.show_help()
            self.render(full=True)
        elif inp == KEY_QUOTE and self.quote:
            self.leave_fields()
            self.quote_window()
            self.render(full=True)
        elif inp == KEY_CUT:
            buf.cut_paragraph()
        elif inp == KEY_PASTE:
            if not buf.paste():
                echo(u'\a')
        elif inp == KEY_DELWORD:
            buf.delete_word_left()
        elif inp == KEY_DELETE:
            buf.delete()
        elif inp == KEY_INSERT:
            buf.overwrite = not buf.overwrite
        elif inp == KEY_REDRAW:
            self.render(full=True)
        return None

    def handle(self, inp):
        """
        Process keystroke ``inp``.

        Returns ``(True, value)`` when the editor exits with ``value``.
        """
        term, buf = self.term, self.buf
        code = inp.code if inp.is_sequence else None
        last_key, self.last_key = self.last_key, str(inp)
        if str(inp) == u'\n' and last_key == u'\r':
            # carriage return followed by line feed is one return key.
            return None

        movement = {
            term.KEY_UP: buf.up,
            term.KEY_DOWN: buf.down,
            term.KEY_HOME: buf.home,
            term.KEY_END: buf.end,
            term.KEY_PGUP: lambda: buf.up(self.text_height - 1),
            term.KEY_PGDOWN: lambda: buf.down(self.text_height - 1),
            term.KEY_LEFT: (buf.word_left if inp.name == 'KEY_CTRL_LEFT'
                            else buf.left),
            term.KEY_RIGHT: (buf.word_right if inp.name == 'KEY_CTRL_RIGHT'
                             else buf.right),
        }
        if code in movement:
            movement[code]()
        elif code == term.KEY_ENTER:
            if not buf.newline():
                echo(u'\a')
        elif code == term.KEY_BACKSPACE:
            buf.backspace()
        elif code == term.KEY_DELETE:
            buf.delete()
        elif code == term.KEY_TAB:
            buf.insert(u' ' * (4 - buf.pos % 4))
        elif code == term.KEY_INSERT:
            buf.overwrite = not buf.overwrite
        elif is_escape(term, inp):
            return self.menu(inp[1:])
        elif inp.name == 'CSI':
            discard_csi(term)
        elif inp.is_sequence:
            # other function keys are ignored.
            pass
        elif len(inp) == 1 and inp < u' ':
            return self.command(str(inp))
        elif inp.isprintable():
            if not buf.insert(str(inp)):
                echo(u'\a')
        return None

    def run(self):
        """ Edit until saved or aborted, return result. """
        result = self.edit()
        self.leave_fields()
        return result

    def edit(self):
        """ Edit until saved or aborted, see :meth:`run`. """
        self.render(full=True)
        while True:
            event, data = self.session.read_events(
                ('refresh', 'input', 'tn3270'), self.autosave_timeout())
            if event == 'refresh':
                self.layout()
                self.render(full=True)
                continue
            if event == 'tn3270':
                result = self.handle_fields(data)
                if result is not None:
                    return result[1]
            if event == 'input':
                self.session.buffer_input(data, pushback=True)
                # process all keys received, such as pasted text, before
                # displaying the result.
                while True:
                    inp = self.term.inkey(0)
                    if not inp:
                        break
                    result = self.handle(inp)
                    if result is not None:
                        return result[1]
            self.autosave()
            self.render()


def main(save_key=None, continue_draft=False, title=None,
         quote=None, quote_header=None, draft_key=None):
    """
    Main Editor procedure.

    When argument ``save_key`` is non-None, the result is saved to the
    user attribute of the same name, and True is returned.  Otherwise
    the text is returned to the caller.  False is returned when aborted.

    :param str continue_draft: initial text, such as a previously saved
        draft.
    :param str title: displayed in the title bar, such as a subject.
    :param str quote: text of a message replied to, whose lines may be
        chosen for quotation by ``^Q``.
    :param str quote_header: line inserted before the first quotation,
        such as ``'On ..., biff wrote:'``.
    :param str draft_key: user attribute to which text is periodically
        saved while editing, so that it may be continued after a lost
        connection.  It is removed when saved or aborted.
    """
    session, term = getsession(), getterminal()
    session.activity = u'editing {0}'.format(title or save_key or u'text')
    editor = Editor(text=continue_draft or u'', title=title, quote=quote,
                    quote_header=quote_header, draft_key=draft_key)
    try:
        content = editor.run()
    except BaseException:
        # such as a lost connection: keep what was typed.
        try:
            editor.autosave(force=True)
        except Exception:  # pylint: disable=W0703
            pass
        raise
    editor.discard_draft()
    echo(term.normal + term.clear)
    if content is False or not save_key:
        return content
    session.user[save_key] = content
    return True
