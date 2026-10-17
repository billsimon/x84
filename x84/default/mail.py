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
``color_bar``, ``color_select``, ``color_header`` and ``color_quote``,
and of the list's columns: ``color_flag``, ``color_author``,
``color_subject`` and ``color_date``, and of unread messages,
``color_author_unread``, ``color_subject_unread`` and
``color_date_unread``.
"""
# local
from x84.bbs import getsession, getterminal, find_user, get_msg, gosub, echo
from x84.bbs import Msg
from x84.bbs.msgbase import (
    list_privmsgs,
    list_sent,
    remove_privmsg,
    remove_sent,
)
from editor import displayable
from msgview import (
    GAP,
    MessageScreen,
    format_date,
    pad,
    prefix_subject,
    quote_header,
    subject_max_length,
    username_max_length,
)

#: user attribute of mail text saved while editing.
DRAFT_KEY = 'mail_draft'

#: user attribute of indices of mail replied.
REPLIED_KEY = 'mail_replied'

#: folders, and the key switching to the other.
FOLDERS = (u'inbox', u'sent')


def forward_text(msg):
    """ Return text of a forwarded message, ``msg``. """
    return u'\r\n'.join((
        u'', u'',
        u'-------- Forwarded message --------',
        u'From: {0}'.format(msg.author),
        u'To: {0}'.format(msg.recipient or u'All'),
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


class Mail(MessageScreen):

    """ Full-screen mail: folder list, reader, and composing. """

    section = 'mail'

    def __init__(self, folder=u'inbox'):
        super().__init__()
        self.mailbox = Mailbox(self.session)
        self.folder = folder
        self.read, self.replied = set(), set()
        self.load()

    def load(self):
        """ Load messages of current folder, keeping the selection. """
        selected = self.selected
        self.entries = self.mailbox.messages(self.folder)
        self.read = self.mailbox.read()
        self.replied = self.mailbox.replied()
        self.index = 0
        self.select(selected)

    def new_message(self, idx):
        """ Reload folder when new mail is received. """
        try:
            msg = get_msg(idx)
        except (KeyError, TypeError, ValueError):
            return
        if msg.recipient == self.mailbox.handle and 'public' not in msg.tags:
            echo(u'\a')
            self.notice = u'New mail from {0}.'.format(msg.author)
            self.load()

    @property
    def other_folder(self):
        """ Name of the folder not displayed. """
        return FOLDERS[1 - FOLDERS.index(self.folder)]

    def switch_folder(self, folder=None):
        """ Display another folder. """
        self.folder = folder or self.other_folder
        self.entries = []
        self.load()

    def is_unread(self, msg):
        """ Whether ``msg`` is unread mail received. """
        return self.folder == u'inbox' and msg.idx not in self.read

    # list

    def list_columns(self):
        """ Return widths of name, subject and date columns. """
        width = self.width - 1
        name = max(4, username_max_length)
        date = 10
        subject = width - 2 - name - GAP - 1 - date
        if subject < 10:
            date = 0
            subject = width - 2 - name - GAP
        return name, max(0, subject), date

    def list_title(self):
        unread = sum(1 for msg in self.entries if self.is_unread(msg))
        title = u' Mail: {0}'.format(self.folder.capitalize())
        if unread:
            title += u', {0} new'.format(unread)
        return title, u'Tab {0} '.format(self.other_folder.capitalize())

    def list_header(self):
        name, subject, date = self.list_columns()
        text = u'  {0}{1}{2}'.format(
            pad(u'From' if self.folder == u'inbox' else u'To', name),
            u' ' * GAP, pad(u'Subject', subject))
        if date:
            text += u' ' + pad(u'Date', date)
        return text

    def list_row(self, msg):
        name, subject, date = self.list_columns()
        unread = u'_unread' if self.is_unread(msg) else u''
        flag = (u'N' if unread else
                u'R' if msg.idx in self.replied else u' ')
        who = msg.author if self.folder == u'inbox' else msg.recipient
        columns = [
            ('flag', flag + u' '),
            ('author' + unread, pad(who or u'', name) + u' ' * GAP),
            ('subject' + unread, pad(msg.subject or u'(no subject)',
                                     subject))]
        if date:
            columns.append(('date' + unread,
                            u' ' + msg.stime.strftime('%Y-%m-%d')))
        return columns

    def list_style(self, msg):
        return 'unread' if self.is_unread(msg) else 'text'

    def list_hints(self):
        hints = [u'Enter Read', u'W Write']
        if self.entries:
            hints += [u'R Reply', u'F Forward', u'D Delete']
        return hints + [u'Tab {0}'.format(self.other_folder.capitalize()),
                        u'Q Quit']

    def list_command(self, inp, msg):
        term = self.term
        code = inp.code if inp.is_sequence else None
        if code == term.KEY_TAB:
            self.switch_folder()
        elif code == term.KEY_DELETE and msg:
            self.delete(msg)
        elif inp.is_sequence:
            pass
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
        return False

    # reader

    def reader_title(self, msg):
        return (u' {0} {1}/{2}'.format(self.folder.capitalize(),
                                       self.index + 1, len(self.entries)),
                u'Esc List ')

    def reader_hints(self, msg):
        return [u'R Reply', u'F Forward', u'D Delete', u'N Next', u'P Prev',
                u'Q List']

    def reader_open(self, msg):
        self.mailbox.mark_read(msg)
        self.read.add(msg.idx)

    def reader_command(self, inp, msg):
        code = inp.code if inp.is_sequence else None
        if inp.is_sequence and code != self.term.KEY_DELETE:
            return False
        if inp.lower() == u'r':
            self.reply(msg)
            self.clear()
        elif inp.lower() == u'f':
            self.forward(msg)
            self.clear()
        elif inp.lower() == u'd' or code == self.term.KEY_DELETE:
            return self.delete(msg)
        return False

    # commands

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

    def write(self, recipient=None, subject=None, body=u'', reply_to=None):
        """
        Compose and send mail, returning whether it was sent.

        The recipient and subject are prompted, unless given; a subject is
        prompted when ``body`` is given, such as of a forwarded message.
        Text of message ``reply_to`` may be quoted.  It becomes the parent
        of a reply when it is mail, too.
        """
        self.clear()
        title = (u'Forward' if body else u'Reply' if reply_to is not None
                 else u'Write mail')
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
        content = self.edit(recipient, subject.strip(), body, reply_to)
        if not content or not content.strip():
            self.notice = u'Not sent.'
            return False
        msg = Msg(recipient=recipient, subject=subject.strip(), body=content)
        msg.tags = set()
        if reply_to is not None and not body and (
                'public' not in reply_to.tags):
            msg.parent = reply_to.idx
        msg.save()
        self.session.send_event('global', ('newmsg', msg.idx))
        self.load()
        self.notice = u'Sent to {0}.'.format(recipient)
        return True

    def edit(self, recipient, subject, body, reply_to):
        """ Edit text of mail, returning it, or False when aborted. """
        session = self.session
        draft = session.user.get(DRAFT_KEY)
        if draft and not body and self.confirm(
                u'Continue your unsent mail from before?'):
            body = draft
        quote = header = None
        if reply_to is not None and not body.strip():
            quote, header = reply_to.body, quote_header(reply_to)
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
                      reply_to=msg):
            self.mailbox.mark_replied(msg)
            self.replied.add(msg.idx)

    def forward(self, msg):
        """ Forward ``msg`` to another user. """
        self.write(subject=prefix_subject(u'Fwd:', msg.subject),
                   body=forward_text(msg))


def main(folder=u'inbox', write=False, recipient=None, reply_to=None):
    """
    Main procedure.

    :param str folder: folder first displayed, ``'inbox'`` or ``'sent'``.
    :param bool write: compose a new message, and return when sent.
    :param str recipient: handle of the recipient, when writing.
    :param int reply_to: index of a message replied privately, such as a
        public message, when writing: its text may be quoted.
    """
    session, term = getsession(), getterminal()
    session.activity = u'reading mail'
    mail = Mail(folder)
    with term.fullscreen():
        if write:
            session.activity = u'writing mail'
            subject, msg = None, None
            if reply_to is not None:
                msg = get_msg(reply_to)
                subject = prefix_subject(u'Re:', msg.subject)
            mail.write(recipient=recipient, subject=subject, reply_to=msg)
        else:
            mail.run()
    echo(term.normal + term.clear)
