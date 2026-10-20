"""
Hacker news script for x/84.

Lets you browse and read popular articles on news.ycombinator.com.
"""
# std imports
import urllib.parse
import collections
import textwrap
import math
import sys
import re

# local
from x84.bbs import getsession, getterminal, echo, LineEditor, get_ini
from common import clicked_entry, is_mouse, menu_clicks

# 3rd-party
import feedparser
import html2text
import requests

#: fontset for SyncTerm emulator
SYNCTERM_FONT = get_ini(
    section='hackernews', key='syncterm_font'
) or 'topaz'

#: color of titlebars when viewing article summary
COLOR_MAIN = get_ini(
    section='hackernews', key='color_main'
) or 'black_on_red'

#: color of titlebars when viewing article
COLOR_VIEW = get_ini(
    section='hackernews', key='color_view'
) or "black_on_magenta"

USER_AGENT = 'Lynx/2.9.2 libwww-FM/2.14 SSL-MM/1.4.1 OpenSSL/3.0.13'
RSS_URL = 'https://news.ycombinator.com/rss'
RSS_TITLE = 'Hacker News'
ARTICLE_LIMIT = 100
REQUEST_TIMEOUT = 10

#: structure defines an article
Article = collections.namedtuple(
    'Article', ['title', 'link', 'comments', 'netloc'])

#: structure defines a key-movement by mapping to ``KEYSET``.
Direction = collections.namedtuple(
    'Direction', ['mapping', 'modifier', 'clamp'])

#: structure defines key movements for :func:`do_movement`
KEYSET = {
    'up': ['KEY_UP', u'k'],
    'sup': ['KEY_SUP', u'K'],
    'pgup': ['KEY_PGUP', u'b'],
    'home': ['KEY_HOME', u'0'],
    'down': ['KEY_DOWN', u'j'],
    'sdown': ['KEY_SDOWN', u'J'],
    'pgdown': ['KEY_PGDOWN', u'f', u' '],
    'end': ['KEY_END', u'G'],
}

#: commands of context help, such as ``[q]uit``, clicked at a 3270.
RE_COMMAND = re.compile(r'\[(\w)\]\w*')

MSG_NOTEXT = ("I'm sorry, the page you requested cannot be displayed in "
              "textmode.  It's probably not worth *reading*, anyway ...")


def get_keyset(term):
    """ Evaluate and return a keymap for given ``term``. """
    return {
        movement: [
            (val if not val.startswith('KEY')
             else getattr(term, val))
            for val in KEYSET[movement]
        ] for movement in KEYSET
    }


def is_movement(inp, keyset, mapping):
    """ Whether ``inp`` is the movement ``mapping`` of ``keyset``. """
    return inp.code in keyset[mapping] or inp in keyset[mapping]


def do_movement(inp, scroll_idx, height, bottom, keyset):
    """ Return new scroll_idx if ``inp`` is a matching movement. """

    directions = (
        # up directions
        Direction(mapping='up',
                  modifier=-1,
                  clamp=lambda idx: max(0, idx)),
        Direction(mapping='sup',
                  modifier=-5,
                  clamp=lambda idx: max(0, idx)),
        Direction(mapping='pgup',
                  modifier=(height - 1) * -1,
                  clamp=lambda idx: max(0, idx)),
        Direction(mapping='home',
                  modifier=scroll_idx * -1,
                  clamp=lambda idx: max(0, idx)),

        # down directions
        Direction(mapping='down',
                  modifier=1,
                  clamp=lambda idx: min(bottom, idx)),
        Direction(mapping='sdown',
                  modifier=5,
                  clamp=lambda idx: min(bottom, idx)),
        Direction(mapping='pgdown',
                  modifier=height - 1,
                  clamp=lambda idx: min(bottom, idx)),
        Direction(mapping='end',
                  modifier=bottom - scroll_idx,
                  clamp=lambda idx: min(bottom, idx)),
    )

    # evaluate and return modified scroll_idx for any matching keyset.
    for direction in directions:
        if is_movement(inp, keyset, direction.mapping):
            return direction.clamp(scroll_idx + direction.modifier)
    return scroll_idx


def locate_commands(term, text):
    """
    Return where the commands of context help ``text`` are displayed.

    ``text`` is as displayed on the last row: its commands, such as
    ``[q]uit``, are clicked at a 3270 terminal (see x84.tn3270).

    :returns: list of ``(row, first column, last column, key)``.
    """
    return [(term.height - 1, match.start(), match.end(), match.group(1))
            for match in RE_COMMAND.finditer(text)]


def press_clicked(session, entries, inp):
    """ Press the key of the command clicked by ``inp``, of ``entries``. """
    key = clicked_entry(entries, inp)
    if key is not None:
        session.buffer_input(key.encode(session.encoding), pushback=True)


def get_article(term, articles, entries=()):
    """
    Prompt for an article number, return matching article.

    An article may be clicked instead, of ``entries``, as returned by
    :func:`locate_summaries`.
    """
    moveto_lastline = term.move(term.height, 0)
    width = term.width
    if term.kind.startswith('ansi'):
        # bah syncterm
        moveto_lastline = term.move(term.height - 1, 0)
        width -= 1
    echo(u''.join((
        moveto_lastline + getattr(term, COLOR_MAIN),
        term.center('', width),
        moveto_lastline,
    )))
    echo(u':: enter article #: ')
    editor = LineEditor(
        width=len(str(ARTICLE_LIMIT)),
        colors={'highlight': getattr(term, COLOR_MAIN)})
    echo(editor.refresh())
    while not (editor.quit or editor.carriage_returned):
        inp = term.inkey()
        if is_mouse(inp):
            article = clicked_entry(entries, inp)
            if isinstance(article, Article):
                return article
            continue
        echo(editor.process_keystroke(inp))
    echo(term.normal)
    if editor.quit:
        # pressed escape
        return None
    article_idx = editor.content
    try:
        return articles[int(article_idx) - 1]
    except (ValueError, IndexError):
        # not an integer, or out of range
        return None


def render_article(term, html_text):
    """ Render and return html text of article as text. """
    html_renderer = html2text.HTML2Text(bodywidth=term.width - 1)
    html_renderer.ignore_links = True
    html_renderer.ignore_images = True

    text_wrapped = []
    for line in html_renderer.handle(html_text).splitlines():
        if len(line) < term.width:
            text_wrapped.append(line)
        else:
            # html2text does not always honor `bodywidth',
            # calculate indentation (line up with previous indent)
            # and textwrap again.
            _subsq_indent = 0
            for _subsq_indent, char in enumerate(line):
                if not char.isspace():
                    break
            _indent = u' ' * _subsq_indent
            text_wrapped.extend(textwrap.wrap(line, term.width - 1,
                                              subsequent_indent=_indent))
    final = [_text.rstrip() for _text in text_wrapped]

    if not final or not any(_line for _line in final):
        # no text was rendered by html2text
        final = [''] * (term.height // 2)
        final.extend(textwrap.wrap(MSG_NOTEXT, term.width - 1))
    return final


def get_article_summaries(term, articles):
    """ Render list of articles summary. """
    results = []
    for idx, article in enumerate(articles, 1):
        _idx = '{0}{1}'.format(idx, term.bold_black('.'))
        _title = article.title
        _netloc = term.bold_black('({0})'.format(article.netloc))
        results.append(u' '.join((_idx, _title, _netloc)))
    return results


def summary_padding(term):
    """ Return rows of each article summary's padding, plus one. """
    # larger terminals get an extra space between summaries
    return 2 if term.height > 25 else 1


def wrap_summary(term, line):
    """ Return article summary ``line``, wrapped to terminal width. """
    # calculate indentation (after first ' ').
    _subsq_indent = ((term.strip_seqs(line[:30]).find(' ') or -1) + 1)
    return term.wrap(line, subsequent_indent=u' ' * _subsq_indent)


def count_summaries(term, start, height, articles):
    """ Return number of articles from ``start`` displayed in full. """
    _vpadd = summary_padding(term)
    count, line_no = 0, 0
    for line in articles[start:]:
        line_no += len(wrap_summary(term, line))
        if line_no > height:
            break
        count += 1
        line_no += _vpadd - 1
    return count


def page_down_summaries(term, scroll_idx, height, articles):
    """ Return scroll index of the article following those in view. """
    return scroll_idx + max(1, count_summaries(
        term, scroll_idx, height, articles))


def page_up_summaries(term, scroll_idx, height, articles):
    """ Return scroll index of the page ending before ``scroll_idx``. """
    start = scroll_idx - 1
    while start > 0 and count_summaries(
            term, start - 1, height, articles) >= scroll_idx - start + 1:
        start -= 1
    return max(0, min(start, scroll_idx))


def render_articles_summary(term, scroll_idx, height, articles, rows=None):
    """
    Render and return articles summary in-view.

    :param dict rows: when given, filled with the index of the article
        displayed by each row of the summary, by row.
    """
    _vpadd = summary_padding(term)
    _endline = term.clear_eol + u'\r\n'

    line_no = 0

    # render all articles,
    output = u''
    _start, _end = scroll_idx, (scroll_idx + (height // _vpadd))
    for idx, line in enumerate(articles[_start:_end], _start):
        # render each line, wrapping to terminal width
        for subline in wrap_summary(term, line):
            if line_no < height:
                output = u''.join((output, subline, _endline))
                if rows is not None:
                    rows[line_no] = idx
                line_no += 1

        # add additional padding between articles if room remains
        if line_no + (_vpadd - 1) < height:
            output = u''.join((output, _endline * (_vpadd - 1)))
            line_no += (_vpadd - 1)

        else:
            # no more room remains; break early.
            break
    # clear text to bottom of screen
    remaining = _endline * (height - line_no)
    output = u''.join((output, remaining))
    return output


def locate_summaries(term, top, rows, articles):
    """
    Return where articles are displayed, of ``rows``, as filled by
    :func:`render_articles_summary` at row ``top``: they are clicked at a
    3270 terminal (see x84.tn3270).

    :returns: list of ``(row, first column, last column, article)``.
    """
    return [(top + row, 0, term.width, articles[idx])
            for row, idx in rows.items()]


def view_article(session, term, url, title):
    """ view an article by target ``url``. """
    # context help
    keyset_help = (u'[r]eturn - (pg)up/down - [{0}%] - [s]hare')

    # display 'fetching ...'
    moveto_lastline = term.move(term.height, 0)
    width = term.width
    if term.kind.startswith('ansi'):
        # bah syncterm
        moveto_lastline = term.move(term.height - 1, 0)
        width -=1
    fetch_txt = 'fetching {0}'.format(url)
    echo(u''.join((moveto_lastline, term.center(fetch_txt[:term.width], width))))

    # perform get request,
    headers = {'User-Agent': USER_AGENT}
    try:
        req = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except Exception as err:
        # a wide variety of exceptions may occur; ssl errors, connect timeouts,
        # read errors, BadStatusLine, it goes on and on.
        e_type, _, _ = sys.exc_info()
        echo(u''.join((
            moveto_lastline,
            term.center('{0}: {1}'.format(e_type, err), width))))
        term.inkey()
        return

    if 200 != req.status_code:
        # display 404, 500, or whatever non-200 code returned.
        echo(u''.join((
            moveto_lastline,
            term.center('failed: status_code={0}'.format(req.status_code), width)
        )))
        term.inkey()
        return

    # decoded by the document's declared (or detected) encoding; output is
    # encoded to the session's encoding as it is written.
    html_text = req.text

    keyset = get_keyset(term)
    _endline = term.clear_eol + u'\r\n'
    bottom = -1
    article_text = u''
    scroll_idx = 0
    last_width, last_height = -1, -1
    do_quit = False
    dirty = True
    width = term.width
    commands = []
    while not do_quit:
        if dirty:
            if last_width != term.width or last_height != term.height:
                # screen size has changed, re-calculate rendered contents
                page_height = term.height - 2
                width = term.width
                if term.kind.startswith('ansi'):
                    # bah syncterm
                    page_height -= 1
                    width -= 1
                article_text = render_article(term, html_text)
                last_width, last_height = term.width, term.height
                bottom = len(article_text) - page_height

            # display titlebar
            echo(term.home)
            echo(getattr(term, COLOR_VIEW)(
                title[:term.width].center(width)))

            # display page at offset
            echo(term.move(1, 0))
            page = slice(scroll_idx, scroll_idx + page_height)
            line_no = 0
            for line_no, line_txt in enumerate(article_text[page]):
                echo(line_txt + _endline)

            # clear (any) remaining lines
            syncterm_sucks = int(not bool(term.kind.startswith('ansi')))
            echo(_endline * (page_height - (line_no + syncterm_sucks)))

            # calculate pct
            _pct = (scroll_idx + page_height) / len(article_text)
            pct = min(int(math.ceil(_pct * 100)), 100)

            # display context help
            moveto_lastline = term.move(term.height, 0)
            if term.kind.startswith('ansi'):
                # bah syncterm
                moveto_lastline = term.move(term.height - 1, 0)
            help_text = keyset_help.format(pct)[:term.width].center(width)
            commands = locate_commands(term, help_text)
            echo(moveto_lastline)
            echo(getattr(term, COLOR_VIEW)(help_text))

            dirty = False

        # block until screen refresh or keyboard input
        event, data = session.read_events(('refresh', 'input'))

        if event == 'refresh':
            dirty = 2
            continue

        elif event == 'input':
            session.buffer_input(data, pushback=True)
            inp = term.inkey(0)
            while inp:
                if is_mouse(inp):
                    press_clicked(session, commands, inp)
                elif inp == u'\x0c':
                    # refresh (^L)
                    dirty = 2
                elif inp.lower() in (u'r', u'q',):
                    do_quit = True
                    break
                elif inp in ('s',):
                    # share url
                    echo(term.move(term.height, 0))
                    echo(term.center(url, width))
                    dirty = True
                    term.inkey()
                else:
                    _idx = do_movement(inp=inp, scroll_idx=scroll_idx,
                                       height=page_height, bottom=bottom,
                                       keyset=keyset)
                    if scroll_idx != _idx:
                        scroll_idx = _idx
                        dirty = 2
                inp = term.inkey(0)


def view_article_summaries(session, term, rss_url, rss_title):
    """ view rss article summary by target ``rss_url``. """
    keyset_help = (u'[q]uit - up/down - [v]iew - [c]omments')

    # fetch rss feed articles
    echo(term.move(term.height // 2, 0))
    echo(term.center('Fetching {0} ...'.format(term.bold(rss_url))).rstrip())
    result = feedparser.parse(rss_url, agent=USER_AGENT)
    if not result.entries:
        # display 404, 500, or whatever non-200 code returned.
        moveto_lastline = term.move(term.height, 0)
        echo(moveto_lastline)
        echo(term.center('failed: status={0}'.format(result.get('status'))))
        term.inkey()
        return

    articles = [Article(title=post.title,
                        link=post.link,
                        comments=post.get('comments', post.link),
                        netloc=urllib.parse.urlparse(post.link).netloc)
                for post in result.entries][:ARTICLE_LIMIT]
    keyset = get_keyset(term)
    bottom = -1
    scroll_idx = 0
    last_width, last_height = -1, -1
    width = term.width
    do_quit = False
    dirty = 2
    entries, commands = [], []
    while not do_quit:
        if dirty == 2:
            # dirty value of '2' is full screen refresh,
            if last_width != term.width or last_height != term.height:
                # screen size has changed, re-calculate rendered contents
                page_height = term.height - 2
                width = term.width
                if term.kind.startswith('ansi'):
                    # bah syncterm
                    page_height -= 1
                    width -= 1
                last_width, last_height = term.width, term.height

                # the last page is the one that ends with the last article.
                article_summary = get_article_summaries(term, articles)
                bottom = page_up_summaries(term, len(article_summary),
                                           page_height, article_summary)
                scroll_idx = min(scroll_idx, bottom)

            # display titlebar
            echo(term.home)
            echo(getattr(term, COLOR_MAIN)(
                rss_title[:term.width].center(width)))
            echo(term.move(1, 0))

            # display page summary of articles by scroll index
            rows = {}
            echo(render_articles_summary(
                term=term, scroll_idx=scroll_idx,
                height=page_height, articles=article_summary, rows=rows))
            entries = locate_summaries(term, 1, rows, articles)

        if dirty > 0:
            # dirty value of '1' or more is context-bar refresh
            moveto_lastline = term.move(term.height, 0)
            if term.kind.startswith('ansi'):
                moveto_lastline = term.move(term.height - 1, 0)
            help_text = keyset_help[:term.width].center(width)
            commands = locate_commands(term, help_text)
            echo(u''.join((
                moveto_lastline,
                getattr(term, COLOR_MAIN)(help_text))
            ))
            dirty = 0

        # block until screen refresh or keyboard input
        event, data = session.read_events(('refresh', 'input'))

        if event == 'refresh':
            dirty = 2
            continue

        elif event == 'input':
            session.buffer_input(data, pushback=True)
            inp = term.inkey(0)
            while inp:
                article = (clicked_entry(entries, inp) if is_mouse(inp)
                           else None)
                if article is not None:
                    # an article clicked is viewed.
                    view_article(session=session, term=term,
                                 url=article.link, title=article.title)
                    dirty = 2
                    break
                elif is_mouse(inp):
                    press_clicked(session, commands, inp)
                elif inp == u'\x0c':
                    # refresh (^L)
                    dirty = 2
                elif inp.lower() in (u'q',):
                    do_quit = True
                    break
                elif inp in (u'v', 'c'):
                    article = get_article(term, articles, entries)
                    if article is not None:
                        url = {u'v': article.link,
                               u'c': article.comments
                               }[inp]
                        view_article(session=session, term=term,
                                     url=url, title=article.title)
                        dirty = 2
                    else:
                        # refresh only input bar, to occlude only our
                        # prompt for an article (selection invalid)
                        dirty = 1
                    break
                else:
                    # pages are of articles, not of rows.
                    if is_movement(inp, keyset, 'pgdown'):
                        _idx = min(bottom, page_down_summaries(
                            term, scroll_idx, page_height, article_summary))
                    elif is_movement(inp, keyset, 'pgup'):
                        _idx = page_up_summaries(
                            term, scroll_idx, page_height, article_summary)
                    else:
                        _idx = do_movement(inp=inp, scroll_idx=scroll_idx,
                                           height=page_height, bottom=bottom,
                                           keyset=keyset)
                    if scroll_idx != _idx:
                        scroll_idx = _idx
                        dirty = 2
                inp = term.inkey(0)


def main(rss_url=None, rss_title=None):
    """ main entry point. """

    session, term = getsession(), getterminal()
    session.activity = 'reading hackernews'
    rss_url = rss_url or RSS_URL
    rss_title = rss_title or RSS_TITLE

    # move to bottom of screen, reset attribute
    echo(term.move(term.height, 0) + term.normal)

    # create a new, empty screen
    echo(u'\r\n' * (term.height + 1))

    with term.hidden_cursor(), menu_clicks(term):
        view_article_summaries(session=session, term=term,
                               rss_url=rss_url, rss_title=rss_title)
