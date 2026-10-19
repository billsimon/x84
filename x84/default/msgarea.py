"""
Message area for x/84.

This script displays the number of new and all messages of the tags
subscribed to, and of new private mail.  Messages are read and posted,
and tags subscribed to are chosen, by the 'board' script, and mail by
the 'mail' script.
"""
# std imports
import collections
import fnmatch

# local
from x84.bbs import (
    syncterm_setfont,
    list_privmsgs,
    getterminal,
    getsession,
    LineEditor,
    list_msgs,
    list_tags,
    get_ini,
    gosub,
    echo,
)
from common import (
    locate_menu_entries,
    read_menu_input,
    menu_clicks,
    render_menu_entries,
    show_description,
    display_banner,
    display_prompt,
    waitprompt,
)

#: banner art displayed in main()
art_file = get_ini(
    section='msgarea', key='art_file'
) or 'art/hx-msg.ans'

#: character encoding of banner art
art_encoding = get_ini(
    section='msgarea', key='art_encoding'
) or 'cp437'

#: preferred fontset for SyncTerm emulator
syncterm_font = get_ini(
    section='msgarea', key='syncterm_font'
) or 'topaz'

#: When set False, menu items are not colorized and render much
#: faster on slower systems (such as raspberry pi).
colored_menu_items = get_ini(
    section='msgarea', key='colored_menu_items', getter='getboolean'
) if get_ini(section='msgarea', key='colored_menu_items') else True

#: color used for description text
color_text = get_ini(
    section='msgarea', key='color_text'
) or 'white'

#: color used for menu key entries
color_highlight = get_ini(
    section='msgarea', key='color_highlight'
) or 'bold_magenta'

#: color used for prompt
color_backlight = get_ini(
    section='msgarea', key='color_prompt',
) or 'magenta_reverse'

#: color used for brackets ``[`` and ``]``
color_lowlight = get_ini(
    section='msgarea', key='color_lowlight'
) or 'bold_black'

def get_menu(messages):
    """ Return list of menu items by given dict ``messages``. """
    MenuItem = collections.namedtuple('MenuItem', ['inp_key', 'text'])
    items = []
    if messages['new']:
        items.extend([
            MenuItem(u'n', u'new ({0})'.format(len(messages['new']))),
            MenuItem(u'm', u'mark all read'),
        ])
    if messages['all']:
        items.append(
            MenuItem(u'a', u'all ({0})'.format(len(messages['all'])))
        )
    items.append(MenuItem(u'v', u'private mail{0}'.format(
        u' ({0} new)'.format(len(messages['new_private']))
        if messages['new_private'] else u'')))
    items.extend([
        MenuItem(u'p', u'post public'),
        MenuItem(u'w', u'write private'),
        MenuItem(u'c', u'change area'),
        MenuItem(u'?', u'help'),
        MenuItem(u'q', u'quit'),
    ])
    return items


def do_mark_as_read(session, message_indicies):
    """ Mark all given messages read. """
    session.user['readmsgs'] = (
        session.user.get('readmsgs', set()) | set(message_indicies)
    )


def get_messages_by_subscription(session, subscription):
    all_tags = list_tags()
    messages = {'all': set(), 'new': set()}
    messages_bytag = {}
    messages_read = session.user.get('readmsgs', set())

    # now occlude all private messages :)
    all_private = list_privmsgs(None)

    # this looks like perl code
    for tag_pattern in subscription:
        messages_bytag[tag_pattern] = collections.defaultdict(set)
        for tag_match in fnmatch.filter(all_tags, tag_pattern):
            msg_indicies = list_msgs(tags=(tag_match,))
            messages['all'].update(msg_indicies - all_private)
            messages_bytag[tag_pattern]['all'].update(msg_indicies - all_private)
        messages_bytag[tag_pattern]['new'] = (
            messages_bytag[tag_pattern]['all'] - messages_read)

    # and make a list of only our own, read by the 'mail' script.
    messages['private'] = list_privmsgs(session.user.handle)
    messages['new_private'] = messages['private'] - messages_read

    # and calculate 'new' messages
    messages['new'] = messages['all'] - messages_read

    return messages, messages_bytag


def describe_message_area(term, subscription, messages_bytags, colors):
    get_num = lambda lookup, tag_pattern, grp: len(lookup[tag_pattern][grp])
    return u''.join((
        colors['highlight'](u'msgarea: '),
        colors['text'](u', ').join((
            u''.join((
                quote(tag_pattern, colors),
                u'({num_new}/{num_all})'.format(
                    num_new=get_num(messages_bytags, tag_pattern, 'new'),
                    num_all=get_num(messages_bytags, tag_pattern, 'all'))
            )) for tag_pattern in subscription)),
        u'\r\n\r\n',
    ))


def quote(txt, colors):
    return u''.join(((u'"'), colors['highlight'](txt), (u'"')))


def get_network_tag_description(term, colors):
    """ Return description text of message networks, if any. """
    server_tags = get_ini('msg', 'server_tags', split=True)
    network_tags = get_ini('msg', 'network_tags', split=True)

    if not (network_tags or server_tags):
        return u''
    return u''.join((
        u'\r\n\r\n',
        colors['text'](u'This board participates in intra-bbs '
                       'messaging, '),
        u''.join((
            colors['text'](u'hosting network messages by tag '),
            u', '.join(quote(tag, colors) for tag in server_tags),
        )) if server_tags else u'',
        (colors['text'](
            u' and ') if (server_tags and network_tags) else u''),
        u''.join((
            colors['text'](u'participating in network messages by tag '),
            u', '.join(quote(tag, colors) for tag in network_tags),
        )) if network_tags else u'',
        u'.',
    ))


def do_describe_message_system(term, colors):
    """ Display help text about message tagging. """

    def describe_group_tags():
        groups = getsession().user.groups
        if not groups:
            return u''

        return u''.join((
            u'\r\n\r\n',
            colors['text'](
                u'Finally, private messages may be shared among groups.  You '
                u'may post messages to any group you are a member of: '),
            colors['text'](
                u', '.join(quote(grp, colors) for grp in groups)),
            colors['text'](u'.')
        ))

    description = u''.join((
        u'\r\n',
        colors['text'](
            u'You can think of tags as a system of providing context to any '
            u'message stored on this system.  A tag might provide the '
            u'general label of the topic of conversation, which may be '
            u'subscribed to.  For example, '),
        quote(u'python', colors),
        colors['text'](
            u' may be used for topics related to the python programming '
            u'language.  This is similar to flicker or gmail tags, or '
            u'hashtags.  Public messages are always tagged '),
        quote(u'public', colors),
        colors['text'](u'.  '),
        get_network_tag_description(term, colors),
        u'\r\n\r\n',
        colors['text'](
            u'Furthermore, glob expressions may be used such as '),
        quote(u'*', colors),
        u' ',
        colors['text']('for all messages, or expression '),
        quote(u'lang-*', colors),
        u' ',
        colors['text']('might subscribe to both '),
        quote(u'lang-python', colors),
        colors['text'](u' and '),
        quote(u'lang-go', colors),
        colors['text'](u'.'),
        describe_group_tags(),
    ))
    return show_description(term, description, color=None)


def main(quick=False):
    """ Main procedure. """

    session, term = getsession(), getterminal()
    session.activity = 'checking for new messages'

    # set syncterm font, if any
    if term.kind.startswith('ansi'):
        echo(syncterm_setfont(syncterm_font))

    colors = dict(
        highlight=lambda txt: txt,
        lowlight=lambda txt: txt,
        backlight=lambda txt: txt,
        text=lambda txt: txt
    ) if not colored_menu_items else dict(
        highlight=getattr(term, color_highlight),
        lowlight=getattr(term, color_lowlight),
        backlight=getattr(term, color_backlight),
        text=getattr(term, color_text))

    yloc = top_margin = 0
    subscription = session.user.get('msg_subscription', [])
    dirty = 2
    entries = []

    while True:
        if dirty == 2:
            # display header art,
            yloc = display_banner(art_file, encoding=art_encoding, center=True)
            xloc = max(0, (term.width // 2) - 40)
            echo(u'\r\n')
            top_margin = yloc = (yloc + 1)

        elif dirty:
            echo(term.move(top_margin, 0) + term.normal + term.clear_eos)
            echo(term.move(top_margin, xloc))

        if dirty:

            if not subscription:
                # choose tags subscribed to, all ('*') when canceled, and
                # loop back again to re-draw and show new messages.
                subscription = gosub('board', subscribe=True)
                if not subscription:
                    subscription = session.user['msg_subscription'] = [u'*']
                dirty = 2
                continue

            messages, messages_bytags = get_messages_by_subscription(
                session, subscription)

            # When quick login ('y') selected in top.py, return immediately
            # when no new messages are matched any longer.
            if quick and not (messages['new'] or messages['new_private']):
                echo(term.move_x(xloc) + u'\r\nNo new messages.\r\n')
                return waitprompt(term)

            txt = describe_message_area(
                term=term, subscription=subscription,
                messages_bytags=messages_bytags, colors=colors)

            yloc = top_margin + show_description(
                term=term, description=txt, color=None,
                subsequent_indent=' ' * len('message area: '))

            # where menu items are, to be chosen by the mouse (at a 3270).
            entries = locate_menu_entries(
                term=term, top_margin=yloc, menu_items=get_menu(messages),
                colors=colors, max_cols=2)
            echo(render_menu_entries(
                term=term, top_margin=yloc,
                menu_items=get_menu(messages),
                colors=colors, max_cols=2))
            echo(display_prompt(term=term, colors=colors))
            echo(colors['backlight'](u' \b'))
            dirty = False

        given_inp = None
        with menu_clicks(term):
            event, data = session.read_events(
                ('refresh', 'newmsg', 'input'))
            if event == 'input':
                # on input, block until carriage return, or a menu item is
                # clicked.
                session.buffer_input(data, pushback=True)
                given_inp = read_menu_input(LineEditor(
                    1, colors={'highlight': colors['backlight']}), entries)

        if event == 'refresh':
            # screen resized, redraw.
            dirty = 2
            continue

        elif event == 'newmsg':
            # When a new message is sent, 'newmsg' event is broadcasted.
            session.flush_event('newmsg')
            nxt_msgs, nxt_bytags = get_messages_by_subscription(
                session, subscription)
            if (nxt_msgs['new'] - messages['new'] or
                    nxt_msgs['new_private'] - messages['new_private']):
                # beep and re-display when a new message has arrived.
                echo(u'\a')
                messages, messages_bytags = nxt_msgs, nxt_bytags
                dirty = True
                continue

        elif event == 'input':
            if given_inp is None:
                # escape/cancel
                continue

            inp = given_inp.strip()
            if inp.lower() in (u'n', u'a', u'p'):
                # read new/all messages, or post one, by the board, where
                # tags subscribed to may be changed, too.
                dirty = 2
                gosub('board', new_only=inp.lower() == u'n',
                      post=inp.lower() == u'p')
                subscription = session.user.get('msg_subscription', [])
            elif inp.lower() == u'm' and messages['new']:
                # mark all messages as read
                dirty = 1
                do_mark_as_read(session, messages['new'])
            elif inp.lower() in (u'v', u'w'):
                # private mail
                dirty = 2
                gosub('mail', write=inp.lower() == u'w')
            elif inp.lower() == u'c':
                # choose tags subscribed to
                subscription = (gosub('board', subscribe=True) or
                                subscription)
                dirty = 2
            elif inp.lower() == u'?':
                # help
                echo(term.move(top_margin, 0) + term.clear_eos)
                do_describe_message_system(term, colors)
                waitprompt(term)
                dirty = 2
            elif inp.lower() == u'q':
                return
            if given_inp:
                # clear out line editor prompt
                echo(colors['backlight'](u'\b \b'))
