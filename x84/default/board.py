"""
Message board for x/84.

Public messages of the tags subscribed to (user attribute
``msg_subscription``, a list of tag patterns such as ``'*'`` or
``'lang-*'``) are listed by thread, newest activity first, or by date.
Unread messages are marked, and may be listed alone.  A message may be
read, replied publicly, its author mailed privately, its tags edited,
and deleted by sysops and tag moderators.  Tags, both of subscription and
of messages posted, are chosen from a list.

Messages are those of the message base, read messages those of user
attribute ``readmsgs``, shared with the message area and mail.  Like the
editor, the board draws only ASCII, so that it displays the same for
utf8 and cp437 terminals.  Its colors may be changed in section
``[board]`` of the configuration: ``color_text``, ``color_unread``,
``color_bar``, ``color_select``, ``color_header`` and ``color_quote``.
"""
# std imports
import collections
import fnmatch

# local
from x84.bbs import (
    getsession,
    getterminal,
    find_user,
    list_tags,
    list_msgs,
    get_ini,
    get_msg,
    DBProxy,
    gosub,
    echo,
    Msg,
)
from x84.bbs.msgbase import list_privmsgs
from msgview import (
    MessageScreen,
    TagPicker,
    pad,
    prefix_subject,
    quote_header,
    subject_max_length,
    username_max_length,
)

#: user attribute of message text saved while editing.
DRAFT_KEY = 'msg_draft'

#: deepest indentation of replies in the thread list.
MAX_DEPTH = 6


def is_moderator(session):
    """ Whether user of ``session`` moderates tags. """
    moderated = get_ini('msg', 'moderated_tags', getter='getboolean')
    tag_moderators = set(get_ini('msg', 'tag_moderators', split=True))
    return bool(moderated and tag_moderators & session.user.groups)


def can_create_tags(session):
    """ Whether new tags may be created: unless tags are moderated. """
    moderated = get_ini('msg', 'moderated_tags', getter='getboolean')
    return not moderated or is_moderator(session)


def can_delete(session):
    """ Whether user of ``session`` may delete messages. """
    return 'sysop' in session.user.groups or is_moderator(session)


def allow_tag(session, msg):
    """
    Whether user is allowed to change tags of message ``msg``.

    A user may change tags of a message when the session's user is:

    * the message author or recipient.
    * a member of sysop or moderator group.
    * a member of any existing tag-matching user group.
    """
    moderated = get_ini('msg', 'moderated_tags', getter='getboolean')
    if not moderated and 'sysop' in session.user.groups:
        return True
    if is_moderator(session):
        return True
    if session.user.handle in (msg.recipient, msg.author):
        return True
    return bool(set(msg.tags) & set(session.user.groups))


def delete_message(msg):
    """ Delete message ``msg`` from the message base. """
    msg.recipient = u''
    msg.subject = u''
    msg.body = u''
    msg.children = set()
    msg.parent = None
    msg.tags = set()
    msg.save()
    with DBProxy('tags') as tag_db:
        for key, values in tag_db.items():
            if msg.idx in values:
                newvalue = values - set([msg.idx])
                if newvalue:
                    tag_db[key] = newvalue
                else:
                    # no more messages by this tag, delete it
                    del tag_db[key]
    for schema in ('privmsg', 'privsent'):
        with DBProxy(schema) as priv_db:
            for key, values in priv_db.items():
                if msg.idx in values:
                    priv_db[key] = values - set([msg.idx])
    with DBProxy('msgbase') as msg_db:
        del msg_db['%d' % int(msg.idx)]


def public_tag_counts():
    """ Return number of public messages, by tag. """
    private = list_privmsgs(None)
    counts = {}
    for tag, indices in DBProxy('tags').items():
        if indices - private:
            counts[tag] = len(indices - private)
    return counts


def subscribed_indices(subscription):
    """ Return set of indices of public messages of ``subscription``. """
    all_tags = list_tags()
    indices = set()
    for pattern in subscription:
        for tag in fnmatch.filter(all_tags, pattern):
            indices |= list_msgs(tags=(tag,))
    return indices - list_privmsgs(None)


def parent_index(msg):
    """ Return index of message replied by ``msg``, or None. """
    try:
        return None if msg.parent is None else int(msg.parent)
    except (TypeError, ValueError):
        return None


def thread_order(messages):
    """
    Return list of (message, depth) of ``messages``, by thread.

    Threads are ordered by their newest message, newest first, and
    replies follow the message replied, oldest first.  A reply to a
    message not listed begins a thread.
    """
    by_idx = dict((msg.idx, msg) for msg in messages)
    replies = collections.defaultdict(list)
    roots = []
    for msg in messages:
        parent = parent_index(msg)
        if parent in by_idx and parent != msg.idx:
            replies[parent].append(msg)
        else:
            roots.append(msg)

    def walk(msg, depth, visited):
        if msg.idx in visited:
            return []
        visited.add(msg.idx)
        result = [(msg, depth)]
        for reply in sorted(replies[msg.idx], key=lambda reply: reply.idx):
            result.extend(walk(reply, depth + 1, visited))
        return result

    visited, threads = set(), []
    for root in roots:
        threads.append(walk(root, 0, visited))
    # replies to each other, in a cycle, as by a damaged database.
    for msg in sorted(messages, key=lambda msg: msg.idx):
        if msg.idx not in visited:
            threads.append(walk(msg, 0, visited))
    threads.sort(key=lambda thread: max(msg.idx for msg, _ in thread),
                 reverse=True)
    return [entry for thread in threads for entry in thread]


class Board(MessageScreen):

    """ Full-screen message board: message list, reader and posting. """

    section = 'board'

    def __init__(self, new_only=False):
        super().__init__()
        self.subscription = (
            self.session.user.get('msg_subscription') or [u'*'])
        self.new_only = new_only
        self.threaded = True
        self.read, self.depth = set(), {}
        self.load()

    # messages

    def load(self, keep=()):
        """
        Load messages of subscription, keeping the selection.

        When listing only unread messages, those of indices ``keep``,
        such as just read, remain listed.
        """
        selected = self.selected
        self.read = self.session.user.get('readmsgs', set())
        indices = subscribed_indices(self.subscription)
        if self.new_only:
            indices -= self.read - set(keep)
        messages = []
        for idx in indices:
            try:
                messages.append(get_msg(idx))
            except KeyError:
                pass
        if self.threaded:
            ordered = thread_order(messages)
        else:
            ordered = [(msg, 0) for msg in sorted(
                messages, key=lambda msg: msg.idx, reverse=True)]
        self.entries = [msg for msg, _ in ordered]
        self.depth = dict((msg.idx, depth) for msg, depth in ordered)
        self.index = 0
        self.select(selected)

    def reload(self):
        """ Load messages again, keeping those listed that were read. """
        self.load(keep=[msg.idx for msg in self.entries])

    def is_unread(self, msg):
        """ Whether ``msg`` is unread. """
        return msg.idx not in self.read

    def mark_read(self, indices):
        """ Mark messages of ``indices`` read. """
        read = self.session.user.get('readmsgs', set())
        if set(indices) - read:
            self.session.user['readmsgs'] = read | set(indices)
        self.read |= set(indices)

    def new_message(self, idx):
        """ Announce new messages of subscription, and list them. """
        try:
            msg = get_msg(idx)
        except (KeyError, TypeError, ValueError):
            return
        if 'public' in msg.tags and idx in subscribed_indices(
                self.subscription):
            echo(u'\a')
            self.notice = u'New message from {0}.'.format(msg.author)
            self.reload()

    # list

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

    def list_title(self):
        unread = sum(1 for msg in self.entries if self.is_unread(msg))
        title = u' Board: {0}'.format(
            u'new messages' if self.new_only else u'all messages')
        if unread and not self.new_only:
            title += u', {0} new'.format(unread)
        return title, u'Tab {0} '.format(u'All' if self.new_only else u'New')

    def list_header(self):
        name, subject, date = self.list_columns()
        text = u'  {0} {1}'.format(pad(u'From', name),
                                   pad(u'Subject', subject))
        if date:
            text += u' ' + pad(u'Date', date)
        return text

    def list_row(self, msg):
        name, subject, date = self.list_columns()
        indent = u'  ' * min(MAX_DEPTH, self.depth.get(msg.idx, 0))
        text = u'{0} {1} {2}'.format(
            u'N' if self.is_unread(msg) else u' ',
            pad(msg.author or u'', name),
            pad(indent + (msg.subject or u'(no subject)'), subject))
        if date:
            text += u' ' + msg.stime.strftime('%Y-%m-%d')
        return text

    def list_style(self, msg):
        return 'unread' if self.is_unread(msg) else 'text'

    def list_empty(self):
        if self.new_only:
            return u'  No new messages.  Tab lists all, P posts.'
        return u'  No messages.  P posts, C changes tags.'

    def list_hints(self):
        hints = [u'Enter Read', u'P Post']
        if self.entries:
            hints.append(u'R Reply')
        hints += [u'C Tags', u'V {0}'.format(
            u'By date' if self.threaded else u'Threads')]
        if any(self.is_unread(msg) for msg in self.entries):
            hints.append(u'M Mark read')
        return hints + [u'Tab {0}'.format(u'All' if self.new_only else u'New'),
                        u'Q Quit']

    def list_command(self, inp, msg):
        code = inp.code if inp.is_sequence else None
        if code == self.term.KEY_TAB:
            self.new_only = not self.new_only
            self.load()
        elif inp.is_sequence:
            pass
        elif inp.lower() == u'p':
            self.post()
        elif inp.lower() == u'r' and msg:
            self.reply(msg)
        elif inp.lower() == u'c':
            self.change_subscription()
        elif inp.lower() == u'v':
            self.threaded = not self.threaded
            self.reload()
        elif inp.lower() == u'm':
            self.mark_all_read()
        return False

    # reader

    def reader_header(self, msg):
        header = super().reader_header(msg)
        tags = sorted(set(msg.tags) - set([u'public']))
        header.insert(3, (u'Tags', u', '.join(tags) or u'(none)'))
        return header

    def reader_title(self, msg):
        return (u' Board {0}/{1}'.format(self.index + 1, len(self.entries)),
                u'Esc List ')

    def reader_hints(self, msg):
        hints = [u'R Reply', u'M Mail author']
        if allow_tag(self.session, msg):
            hints.append(u'T Tags')
        if can_delete(self.session):
            hints.append(u'D Delete')
        return hints + [u'N Next', u'P Prev', u'Q List']

    def reader_open(self, msg):
        self.mark_read([msg.idx])

    def reader_command(self, inp, msg):
        if inp.is_sequence:
            return False
        if inp.lower() == u'r':
            self.reply(msg)
            self.clear()
        elif inp.lower() == u'm':
            self.mail_author(msg)
            self.clear()
        elif inp.lower() == u't' and allow_tag(self.session, msg):
            self.edit_tags(msg)
            self.clear()
        elif inp.lower() == u'd' and can_delete(self.session):
            return self.delete(msg)
        return False

    # commands

    def mark_all_read(self):
        """ Mark all messages listed read, when confirmed. """
        unread = [msg.idx for msg in self.entries if self.is_unread(msg)]
        if unread and self.confirm(u'Mark {0} message{1} read?'.format(
                len(unread), u'' if len(unread) == 1 else u's')):
            self.mark_read(unread)
            self.notice = u'Marked read.'

    def pick_tags(self, title, chosen):
        """ Return tags chosen for a message, or None when canceled. """
        counts = public_tag_counts()
        counts.pop(u'public', None)
        allow_new = can_create_tags(self.session)

        def validate(tag):
            if any(ucs in tag for ucs in u'*?[],'):
                return u'Tags may not contain * ? [ ] or commas.'
            if tag == u'public':
                return u'Public messages are always tagged public.'
            return None
        picker = TagPicker(title, counts, chosen=chosen, allow_new=allow_new,
                           validate=validate)
        result = picker.run()
        self.clear()
        return result

    def change_subscription(self):
        """ Choose tags subscribed to. """
        result = choose_subscription(self.session, self.subscription)
        self.clear()
        if result is not None:
            self.subscription = result
            self.load()

    def edit_tags(self, msg):
        """ Change tags of ``msg``. """
        chosen = self.pick_tags(u'Tags of "{0}"'.format(msg.subject),
                                set(msg.tags) - set([u'public']))
        if chosen is None:
            return
        tags = set(chosen)
        if 'public' in msg.tags:
            tags.add(u'public')
        msg.tags = tags
        msg.save()
        self.reload()
        self.notice = u'Tags changed.'

    def delete(self, msg):
        """ Delete ``msg``, when confirmed. """
        if not self.confirm(u'Delete "{0}" for everyone?'.format(
                pad(msg.subject or u'(no subject)', 30).rstrip())):
            return False
        index = self.index
        delete_message(msg)
        self.reload()
        self.index = min(index, max(0, len(self.entries) - 1))
        self.notice = u'Deleted.'
        return True

    def mail_author(self, msg):
        """ Reply privately to the author of ``msg``. """
        handle = find_user(msg.author or u'')
        if not handle:
            self.notice = u'{0} is no longer a user.'.format(msg.author)
            return
        gosub('mail', write=True, recipient=handle, reply_to=msg.idx)
        self.layout()

    def reply(self, msg):
        """ Reply publicly to ``msg``. """
        self.post(reply_to=msg)

    def post(self, reply_to=None):
        """
        Compose and post a public message, returning whether posted.

        A new message is addressed to all, or a user, and its tags are
        chosen.  A reply is addressed to the author of message
        ``reply_to``, whose text may be quoted, and has its tags.
        """
        self.clear()
        echo(self.draw_bar(0, u' Reply' if reply_to else u' Post message') +
             self.draw_bar(self.height - 1,
                           u' Enter accepts, Esc cancels' if not reply_to
                           else u''))
        if reply_to is None:
            recipient = self.prompt_recipient(2, allow_empty=True)
            if recipient is None:
                return False
            echo(self.term.move(2, 0) + u'     To: ' + (recipient or u'All') +
                 self.term.clear_eol)
            subject = self.prompt_line(3, u'Subject: ', u'',
                                       subject_max_length)
            if not subject or not subject.strip():
                self.notice = u'Not posted.'
                return False
            subject = subject.strip()
        else:
            recipient = reply_to.author
            subject = prefix_subject(u'Re:', reply_to.subject)
        content = self.edit(recipient, subject, reply_to)
        if not content or not content.strip():
            self.notice = u'Not posted.'
            return False
        if reply_to is None:
            tags = self.pick_tags(u'Tags of your message (optional)', ())
            if tags is None:
                # keep what was written, to be continued when next posted.
                self.session.user[DRAFT_KEY] = content
                self.notice = u'Not posted; your text is kept as a draft.'
                return False
        else:
            tags = reply_to.tags
        msg = Msg(recipient=recipient or None, subject=subject, body=content)
        msg.tags = set(tags) | set([u'public'])
        if reply_to is not None:
            msg.parent = reply_to.idx
        msg.save()
        self.mark_read([msg.idx])
        self.session.send_event('global', ('newmsg', msg.idx))
        self.reload()
        self.select(msg)
        self.notice = u'Posted.'
        return True

    def edit(self, recipient, subject, reply_to):
        """ Edit text of a message, returning it, or False when aborted. """
        body = u''
        draft = self.session.user.get(DRAFT_KEY)
        if draft and self.confirm(
                u'Continue your unsent message from before?'):
            body = draft
        quote = header = None
        if reply_to is not None:
            quote, header = reply_to.body, quote_header(reply_to)
        content = gosub('editor', continue_draft=body,
                        title=u'{0}  To: {1}'.format(subject,
                                                     recipient or u'All'),
                        quote=quote, quote_header=header,
                        draft_key=DRAFT_KEY)
        self.layout()
        self.clear()
        return content


def choose_subscription(session, subscription):
    """
    Choose tags subscribed to, saving and returning them.

    Returns None when canceled.
    """
    counts = public_tag_counts()
    all_tags = list_tags()
    counts[u'*'] = len(subscribed_indices([u'*']))

    def validate(pattern):
        if not fnmatch.filter(all_tags, pattern):
            return u'No tags match {0}.'.format(pattern)
        return None
    notice = None
    while True:
        picker = TagPicker(u'Choose subscription tags', counts,
                           chosen=subscription or [u'*'], validate=validate)
        picker.notice = notice
        result = picker.run()
        if result is None:
            return None
        if result:
            session.user['msg_subscription'] = result
            return result
        subscription = []
        notice = u'Choose at least one tag, or * for all.'


def main(new_only=False, post=False, subscribe=False):
    """
    Main procedure.

    :param bool new_only: list only unread messages.
    :param bool post: post a new message, then list messages.
    :param bool subscribe: choose tags subscribed to, and return them
        (or None, when canceled).
    """
    session, term = getsession(), getterminal()
    session.activity = u'reading messages'
    result = None
    with term.fullscreen():
        if subscribe:
            session.activity = u'choosing tags'
            result = choose_subscription(
                session, session.user.get('msg_subscription'))
        elif post:
            # once posted, the message is displayed among the others.
            session.activity = u'posting a message'
            board = Board()
            if board.post():
                board.run()
        else:
            Board(new_only=new_only).run()
    echo(term.normal + term.clear)
    return result
