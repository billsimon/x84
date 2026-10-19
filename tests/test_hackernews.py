""" Tests of the hackernews script of the default board. """
# 3rd-party
import pytest

# local
from tests.screens import PAUSE, run_script

feedparser = pytest.importorskip('feedparser')
pytest.importorskip('html2text')


def summaries(hackernews, term):
    """ Return summaries of 30 articles, some wrapping to two rows. """
    articles = [hackernews.Article(
        title=u'Article number {0} {1}'.format(idx, u'long ' * (idx % 3 * 9)),
        link=u'http://example.com', comments=u'', netloc=u'example.com')
        for idx in range(1, 31)]
    return hackernews.get_article_summaries(term, articles)


def displayed(hackernews, term, scroll_idx, height, lines):
    """ Return the numbers of the articles displayed in full. """
    output = hackernews.render_articles_summary(
        term=term, scroll_idx=scroll_idx, height=height, articles=lines)
    rows = term.strip_seqs(output).split(u'\r\n')[:height]
    text = u' '.join(row.strip() for row in rows)
    # each article ends with its netloc, displayed only when in full.
    return [int(article.split()[0]) for article in
            text.split(u'Article number ')[1:]
            if u'(example.com)' in article]


@pytest.mark.parametrize('rows', [24, 42])
def test_summaries_page_by_articles(script, session, rows):
    # paging down displays the articles following those displayed, none
    # skipped, and paging up returns to the pages before.
    hackernews = script('hackernews')
    term = session.term
    term._rows = rows
    height = rows - 2
    lines = summaries(hackernews, term)
    bottom = hackernews.page_up_summaries(term, len(lines), height, lines)
    assert displayed(hackernews, term, bottom, height, lines)[-1] == 30
    starts, scroll_idx = [], 0
    seen = []
    while True:
        starts.append(scroll_idx)
        shown = displayed(hackernews, term, scroll_idx, height, lines)
        assert shown[0] == scroll_idx + 1
        assert len(shown) == hackernews.count_summaries(
            term, scroll_idx, height, lines)
        seen.extend(shown)
        if scroll_idx == bottom:
            break
        scroll_idx = min(bottom, hackernews.page_down_summaries(
            term, scroll_idx, height, lines))
    assert sorted(set(seen)) == list(range(1, 31))
    assert len(starts) > 1
    for previous, start in zip(starts[-2::-1], starts[:0:-1]):
        assert hackernews.page_up_summaries(
            term, start, height, lines) <= previous
    assert hackernews.page_up_summaries(term, 0, height, lines) == 0


class Response(object):

    """ A response of requests, of an article. """

    status_code = 200
    text = u'<p>the article</p>'


def click(row, col):
    """ Return a click of the mouse, as by ENTER at a 3270. """
    return u'\x1b[<0;{0};{1}M\x1b[<0;{0};{1}m'.format(col + 1, row + 1)


def test_clicks_of_3270(users, engine, script, session, monkeypatch):
    # at a 3270, ENTER is a click of the mouse where the cursor is: an
    # article clicked is viewed, and commands of the context help, such as
    # [q]uit, are their keys; an article is clicked for [c]omments, too.
    hackernews = script('hackernews')
    session.session.kind = 'tn3270'
    # as answered by the tn3270 server, to queries of mouse modes.
    session.session.terminal._dec_mode_cache.update({1000: 2, 1006: 2})
    entries = [feedparser.FeedParserDict(
        title=u'Article {0}'.format(idx),
        link=u'http://example.com/{0}'.format(idx),
        comments=u'http://example.com/comments/{0}'.format(idx))
        for idx in range(1, 31)]
    monkeypatch.setattr(hackernews.feedparser, 'parse',
                        lambda url, agent: feedparser.FeedParserDict(
                            entries=entries))
    fetched = []

    def fake_get(url, headers, timeout):
        fetched.append(url)
        return Response()
    monkeypatch.setattr(hackernews.requests, 'get', fake_get)

    def command(text, name):
        # as centered on the last row.
        return click(23, text.center(80).index(name) + 1)
    summary_help = u'[q]uit - up/down - [v]iew - [c]omments'
    article_help = u'[r]eturn - (pg)up/down - [100%] - [s]hare'
    # the list begins at the second row, an article to each row.
    keys = [click(2, 5), PAUSE, command(article_help, u'[r]eturn'), PAUSE,
            command(summary_help, u'[c]omments'), PAUSE, click(3, 30), PAUSE,
            command(article_help, u'[r]eturn'), PAUSE,
            command(summary_help, u'[q]uit'),
            # should a click not be taken: escape a prompt, and quit.
            2.0, u'\x1b', PAUSE, u'r', PAUSE, u'q']
    output = run_script(engine, hackernews, keys)
    assert u'\x1b[?1006;1000h' in output.replace(u'\x1b[?1000;1006h',
                                                 u'\x1b[?1006;1000h')
    assert fetched == [u'http://example.com/2',
                       u'http://example.com/comments/3']
    assert u'enter article #' in output
