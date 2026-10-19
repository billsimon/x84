""" Tests of the hackernews script of the default board. """
# 3rd-party
import pytest

pytest.importorskip('feedparser')
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
