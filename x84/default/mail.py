"""
Private mail for x/84.

Mail is kept in the message base as private messages, those without the
``public`` tag, listed by recipient (database ``privmsg``) and by author
(``privsent``).  Mail written by earlier versions of x/84 is read here
alike.  Deleting a message removes it only from the folder of the user
who deleted it, and read messages are those of user attribute
``readmsgs``, shared with the message area.

The inbox and sent folders list messages, newest first.  A message may be
read, replied, forwarded or deleted from either the list or the reader.
Like the editor, mail draws only ASCII, so that it displays the same for
utf8 and cp437 terminals.  Its colors may be changed in section
``[mail]`` of the configuration: ``color_text``, ``color_unread``,
``color_bar``, ``color_select``, ``color_header`` and ``color_quote``.
"""
# std imports
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
    get_msg,
    gosub,
    echo,
    Msg,
)
from x84.bbs.msgbase import (
    list_privmsgs,
    list_sent,
    remove_privmsg,
    remove_sent,
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

#: user attribute of mail text saved while editing.
DRAFT_KEY = 'mail_draft'

#: user attribute of indices of mail replied.
REPLIED_KEY = 'mail_replied'

#: folders, and the key switching to the other.
FOLDERS = (u'inbox', u'sent')

#: default colors, by name of terminal capability.
DEFAULT_COLORS = {
    'text': 'normal',
    'unread': 'bold',
    'bar': 'bold_white_on_blue',
    'select': 'reverse',
    'header': 'bold',
    'quote': 'bold_cyan',
}

#: color sequences, as decoded from pipe codes of message text.
RE_SGR = re.compile(r'\x1b\[[0-9;]*m')

KEY_REDRAW = ctrl('l')


def get_colors(term):
    """ Return dict of color functions, by name, from configuration. """
    colors = {}
    for name, default in DEFAULT_COLORS.items():
        attr = get_ini(section='mail',
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


def forward_text(msg):
    """ Return text of a forwarded message, ``msg``. """
    return u'\r\n'.join((
        u'', u'',
        u'-------- Forwarded message --------',
        u'From: {0}'.format(msg.author),
        u'To: {0}'.format(msg.recipient),
        u'Date: {0}'.format(format_date(msg)),
        u'Subject: {0}'.format(msg.subject),
        u'',
        msg.body))


class Mailbox(object):

    """ Mail of the session user, by folder. """

    def __init__(self, session):
        self.session = session
        self.handle = session.user.handle

    def messages(self, folder):
        """ Return list of messages of ``folder``, newest first. """
        indices = (list_privmsgs(self.handle) if folder == u'inbox'
                   else list_sent(self.handle))
        result = []
        for idx in sorted(indices, reverse=True):
            try:
                result.append(get_msg(idx))
            except KeyError:
                # removed from the message base, such as by a sysop.
                pass
        return result

    def unread(self):
        """ Return set of indices of unread mail received. """
        return list_privmsgs(self.handle) - self.read()

    def read(self):
        """ Return set of indices of messages read. """
        return self.session.user.get('readmsgs', set())

    def mark_read(self, msg):
        """ Mark message ``msg`` read. """
        read = self.read()
        if msg.idx not in read:
            self.session.user['readmsgs'] = read | set([msg.idx])

    def replied(self):
        """ Return set of indices of messages replied. """
        return self.session.user.get(REPLIED_KEY, set())

    def mark_replied(self, msg):
        """ Mark message ``msg`` replied. """
        self.session.user[REPLIED_KEY] = self.replied() | set([msg.idx])

    def remove(self, folder, msg):
        """ Remove message ``msg`` from ``folder``. """
        if folder == u'inbox':
            remove_privmsg(self.handle, msg.idx)
        else:
            remove_sent(self.handle, msg.idx)


class Mail(object):

    """ Full-screen mail: folder list, reader, and composing. """

    # pylint: disable=R0902
    #         Too many instance attributes

    def __init__(self, folder=u'inbox'):
        self.session, self.term = getsession(), getterminal()
        self.colors = get_colors(self.term)
        self.mailbox = Mailbox(self.session)
        self.folder = folder
        self.entries, self.read, self.replied = [], set(), set()
        self.index = self.top = 0
        self.notice = None
        self.screen = {}
        self.width = self.height = 0
        self.layout()
        self.load()

    # layout and display

    def layout(self):
        """ Size display to terminal. """
        self.width, self.height = self.term.width, self.term.height

    @property
    def list_height(self):
        """ Number of rows of the folder list. """
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
        if prior is None or prior_style == 'raw' or (
                text_width(prior) > text_width(text)):
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
        """ Return sequence to display a title or status bar. """
        return self.draw_row(yloc, 'bar', fit(left, right, self.width - 1))

    def draw_status(self, hints, right=u''):
        """ Return sequence to display status bar of key ``hints``. """
        if self.notice:
            return self.draw_bar(self.height - 1, u' ' + self.notice)
        left = u''
        for hint in hints:
            if text_width(left + u' ' + hint + right) >= self.width - 1:
                break
            left += u' ' + hint
        return self.draw_bar(self.height - 1, left, right)

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

    def read_key(self):
        """
        Return next keystroke, or None after other events.

        The terminal may be resized, and new mail may arrive, while
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
            self.new_mail(data)
        elif event == 'input':
            self.session.buffer_input(data, pushback=True)
            return self.term.inkey(0) or None
        return None

    def new_mail(self, data):
        """ Reload folder when new mail is received. """
        try:
            msg = get_msg(data)
        except (KeyError, TypeError, ValueError):
            return
        if msg.recipient == self.mailbox.handle and 'public' not in msg.tags:
            echo(u'\a')
            self.notice = u'New mail from {0}.'.format(msg.author)
            self.load()

    def code(self, inp):
        """ Return keycode of keystroke ``inp``, or None. """
        if inp.is_sequence:
            if inp.name == 'CSI':
                discard_csi(self.term)
            return inp.code
        return None

    # folder list

    def load(self):
        """ Load messages of current folder, keeping the selection. """
        selected = self.selected
        self.entries = self.mailbox.messages(self.folder)
        self.read = self.mailbox.read()
        self.replied = self.mailbox.replied()
        self.index = 0
        for index, msg in enumerate(self.entries):
            if selected is not None and msg.idx == selected.idx:
                self.index = index

    @property
    def selected(self):
        """ Selected message, or None. """
        if 0 <= self.index < len(self.entries):
            return self.entries[self.index]
        return None

    def is_unread(self, msg):
        """ Whether ``msg`` is unread mail received. """
        return self.folder == u'inbox' and msg.idx not in self.read

    def list_columns(self):
        """ Return widths of name, subject and date columns. """
        width = self.width - 1
        name = max(4, username_max_length)
        date = 10
        subject = width - 2 - name - 1 - 1 - date
        if subject < 10:
            date = 0
            subject = width - 2 - name - 1
        return name, max(0, subject), date

    def list_row(self, msg):
        """ Return text of folder list row of ``msg``. """
        name, subject, date = self.list_columns()
        flag = (u'N' if self.is_unread(msg) else
                u'R' if msg.idx in self.replied else u' ')
        who = msg.author if self.folder == u'inbox' else msg.recipient
        text = u'{0} {1} {2}'.format(flag, pad(who or u'', name),
                                     pad(msg.subject or u'(no subject)',
                                         subject))
        if date:
            text += u' ' + msg.stime.strftime('%Y-%m-%d')
        return text

    def list_header(self):
        """ Return text of folder list column headings. """
        name, subject, date = self.list_columns()
        text = u'  {0} {1}'.format(
            pad(u'From' if self.folder == u'inbox' else u'To', name),
            pad(u'Subject', subject))
        if date:
            text += u' ' + pad(u'Date', date)
        return text

    def render_list(self):
        """ Display folder list. """
        height = self.list_height
        if self.index < self.top or self.index >= self.top + height:
            self.top = max(0, self.index - height // 2)
        unread = sum(1 for msg in self.entries if self.is_unread(msg))
        title = u' Mail: {0}'.format(self.folder.capitalize())
        if unread:
            title += u', {0} new'.format(unread)
        other = FOLDERS[1 - FOLDERS.index(self.folder)]
        output = [self.draw_bar(0, title, u'Tab {0} '.format(
            other.capitalize())),
            self.draw_row(1, 'header', self.list_header())]
        for yloc in range(height):
            idx = self.top + yloc
            text, style = u'', 'text'
            if idx < len(self.entries):
                msg = self.entries[idx]
                text = self.list_row(msg)
                style = ('select' if idx == self.index else
                         'unread' if self.is_unread(msg) else 'text')
            elif idx == 0:
                text = u'  No messages.'
            output.append(self.draw_row(2 + yloc, style, text))
        hints = [u'Enter Read', u'W Write']
        if self.entries:
            hints += [u'R Reply', u'F Forward', u'D Delete']
        hints += [u'Tab {0}'.format(other.capitalize()), u'Q Quit']
        right = u'{0}/{1} '.format(
            self.index + 1, len(self.entries)) if self.entries else u''
        output.append(self.draw_status(hints, right))
        output.append(self.term.move(2 + self.index - self.top, 0))
        echo(u''.join(output))

    def switch_folder(self, folder=None):
        """ Display another folder. """
        self.folder = folder or FOLDERS[1 - FOLDERS.index(self.folder)]
        self.entries = []
        self.load()

    def run(self):
        """ Display folder list until quit. """
        term = self.term
        self.clear()
        while True:
            self.render_list()
            inp = self.read_key()
            if inp is None:
                continue
            self.notice = None
            code = self.code(inp)
            last = len(self.entries) - 1
            msg = self.selected
            if code == term.KEY_UP:
                self.index = max(0, self.index - 1)
            elif code == term.KEY_DOWN:
                self.index = max(0, min(last, self.index + 1))
            elif code == term.KEY_PGUP:
                self.index = max(0, self.index - self.list_height)
            elif code == term.KEY_PGDOWN:
                self.index = max(0, min(last, self.index + self.list_height))
            elif code == term.KEY_HOME:
                self.index = 0
            elif code == term.KEY_END:
                self.index = max(0, last)
            elif code in (term.KEY_ENTER, term.KEY_RIGHT) and msg:
                self.reader()
            elif code == term.KEY_TAB:
                self.switch_folder()
            elif code == term.KEY_DELETE and msg:
                self.delete(msg)
            elif is_escape(term, inp) or inp.lower() == u'q':
                return
            elif inp == KEY_REDRAW:
                self.clear()
            elif inp.lower() == u'i':
                self.switch_folder(u'inbox')
            elif inp.lower() == u's':
                self.switch_folder(u'sent')
            elif inp.lower() == u'w':
                self.write()
            elif inp.lower() == u'r' and msg:
                self.reply(msg)
            elif inp.lower() == u'f' and msg:
                self.forward(msg)
            elif inp.lower() == u'd' and msg:
                self.delete(msg)

    # reader

    def reader_rows(self, msg):
        """ Return list of (style, text) rows displaying ``msg``. """
        term, width = self.term, self.width - 1
        rows = [
            ('header', u'   From: {0}'.format(msg.author)),
            ('header', u'     To: {0}'.format(msg.recipient)),
            ('header', u'   Date: {0}'.format(format_date(msg))),
            ('header', u'Subject: {0}'.format(
                msg.subject or u'(no subject)')),
            ('text', u''),
        ]
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
        msg, rows, top = None, [], 0
        self.clear()
        while True:
            if self.selected is None:
                return
            if msg is None or msg.idx != self.selected.idx:
                msg, top = self.selected, 0
                self.mailbox.mark_read(msg)
                self.read.add(msg.idx)
                rows, rows_width = self.reader_rows(msg), self.width
            elif rows_width != self.width:
                rows, rows_width = self.reader_rows(msg), self.width
            height = self.text_height
            last_top = max(0, len(rows) - height)
            top = min(top, last_top)
            output = [self.draw_bar(
                0, u' {0} {1}/{2}'.format(self.folder.capitalize(),
                                         self.index + 1, len(self.entries)),
                u'Esc List ')]
            for yloc in range(height):
                idx = top + yloc
                style, text = rows[idx] if idx < len(rows) else ('text', u'')
                output.append(self.draw_raw(1 + yloc, text) if style == 'raw'
                              else self.draw_row(1 + yloc, style, text))
            output.append(self.draw_status(
                [u'R Reply', u'F Forward', u'D Delete', u'N Next', u'P Prev',
                 u'Q List'],
                u'{0}%'.format(100 * min(len(rows), top + height) //
                               max(1, len(rows))).rjust(5) + u' '))
            output.append(term.move(self.height - 1, 0))
            echo(u''.join(output))

            inp = self.read_key()
            if inp is None:
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
            elif is_escape(term, inp) or inp.lower() == u'q':
                return
            elif inp == KEY_REDRAW:
                self.clear()
            elif inp.lower() == u'r':
                self.reply(msg)
                self.clear()
            elif inp.lower() == u'f':
                self.forward(msg)
                self.clear()
            elif inp.lower() == u'd' or code == term.KEY_DELETE:
                if self.delete(msg):
                    return

    def step(self, delta):
        """ Select next (or previous) message, return whether moved. """
        index = self.index + delta
        if 0 <= index < len(self.entries):
            self.index = index
            return True
        return False

    def delete(self, msg):
        """ Delete ``msg`` from current folder, when confirmed. """
        if not self.confirm(u'Delete "{0}"?'.format(
                displayable(msg.subject or u'(no subject)', 30))):
            return False
        self.mailbox.remove(self.folder, msg)
        index = self.index
        self.load()
        self.index = min(index, max(0, len(self.entries) - 1))
        self.notice = u'Deleted.'
        return True

    # composing

    def prompt_line(self, yloc, label, content, width):
        """ Prompt for a line of text, returning None when canceled. """
        echo(self.term.move(yloc, 0) + self.term.normal + label +
             self.term.clear_eol)
        self.screen[yloc] = (None, None)
        return LineEditor(width, content,
                          colors={'highlight': self.colors['select']}).read()

    def prompt_recipient(self, yloc, handle=u''):
        """ Prompt for handle of a user, returning None when canceled. """
        while True:
            handle = self.prompt_line(yloc, u'     To: ', handle,
                                      username_max_length)
            if not handle or not handle.strip():
                return None
            handle = handle.strip()
            match = find_user(handle)
            if match:
                return match
            users = list_users()
            by_lower = dict((user.lower(), user) for user in users)
            close = difflib.get_close_matches(handle.lower(), by_lower, n=1)
            if close and self.confirm(u'No user {0}; send to {1}?'.format(
                    handle, by_lower[close[0]])):
                return by_lower[close[0]]
            if not close:
                self.prompt(u'No user {0}. Press any key.'.format(handle))

    def write(self, recipient=None, subject=None, body=u'', parent=None):
        """
        Compose and send mail, returning whether it was sent.

        The recipient and subject are prompted, unless given.
        """
        self.clear()
        title = (u'Reply' if parent is not None and not body else
                 u'Forward' if body else u'Write mail')
        echo(self.draw_bar(0, u' ' + title) + self.draw_bar(
            self.height - 1, u' Enter accepts, Esc cancels'))
        if recipient is None:
            recipient = self.prompt_recipient(2)
            if recipient is None:
                return False
        echo(self.term.move(2, 0) + u'     To: ' + recipient +
             self.term.clear_eol)
        if subject is None or body:
            subject = self.prompt_line(3, u'Subject: ', subject or u'',
                                       subject_max_length)
            if subject is None:
                return False
        content = self.edit(recipient, subject.strip(), body, parent)
        if not content or not content.strip():
            self.notice = u'Not sent.'
            return False
        msg = Msg(recipient=recipient, subject=subject.strip(), body=content)
        msg.tags = set()
        if parent is not None and not body:
            msg.parent = parent.idx
        msg.save()
        self.session.send_event('global', ('newmsg', msg.idx))
        self.load()
        self.notice = u'Sent to {0}.'.format(recipient)
        return True

    def edit(self, recipient, subject, body, parent):
        """ Edit text of mail, returning it, or False when aborted. """
        session = self.session
        draft = session.user.get(DRAFT_KEY)
        if draft and not body and self.confirm(
                u'Continue your unsent mail from before?'):
            body = draft
        quote = header = None
        if parent is not None and not body.strip():
            quote, header = parent.body, quote_header(parent)
        content = gosub('editor', continue_draft=body,
                        title=u'{0}  To: {1}'.format(
                            subject or u'(no subject)', recipient),
                        quote=quote, quote_header=header,
                        draft_key=DRAFT_KEY)
        self.layout()
        self.clear()
        return content

    def reply(self, msg):
        """ Reply to ``msg``. """
        recipient = (msg.recipient if msg.author == self.mailbox.handle
                     else msg.author)
        handle = find_user(recipient or u'')
        if not handle:
            self.notice = u'{0} is no longer a user.'.format(recipient)
            return
        if self.write(recipient=handle,
                      subject=prefix_subject(u'Re:', msg.subject),
                      parent=msg):
            self.mailbox.mark_replied(msg)
            self.replied.add(msg.idx)

    def forward(self, msg):
        """ Forward ``msg`` to another user. """
        self.write(subject=prefix_subject(u'Fwd:', msg.subject),
                   body=forward_text(msg), parent=msg)


def main(folder=u'inbox', write=False, recipient=None):
    """
    Main procedure.

    :param str folder: folder first displayed, ``'inbox'`` or ``'sent'``.
    :param bool write: compose a new message, and return when sent.
    :param str recipient: handle of the recipient, when writing.
    """
    session, term = getsession(), getterminal()
    session.activity = u'reading mail'
    mail = Mail(folder)
    with term.fullscreen():
        if write:
            session.activity = u'writing mail'
            mail.write(recipient=recipient)
        else:
            mail.run()
    echo(term.normal + term.clear)
