""" Tests for the message board script, and its tag picker. """
# std imports
import time

# 3rd-party
import pytest

# local
from x84.bbs.dbproxy import DBProxy
from x84.bbs.msgbase import MSGDB, Msg, get_msg, list_msgs
from tests.screens import PAUSE, gosub_editor, run_script

DOWN, UP, ESC = u'\x1b[B', u'\x1b[A', u'\x1b'


def post(author, subject, tags=(u'bttf',), parent=None, recipient=None,
         body=u'text'):
    msg = Msg(recipient=recipient, subject=subject, body=body)
    msg.author = author
    msg.tags = set(tags) | {u'public'}
    msg.parent = parent
    msg.save(send_net=False)
    return msg


def private(author, recipient, subject):
    msg = Msg(recipient=recipient, subject=subject, body=u'secret')
    msg.author, msg.tags = author, set()
    msg.save(send_net=False)
    return msg


def fake(idx, parent=None):
    msg = Msg()
    msg.idx, msg.parent = idx, parent
    return msg


# threads


def test_thread_order(script):
    board = script('board')
    # 0 <- 1 <- 3, 0 <- 4, and 2 alone; 5 replies to a message not listed.
    msgs = [fake(0), fake(1, 0), fake(2), fake(3, 1), fake(4, 0), fake(5, 9)]
    order = [(msg.idx, depth) for msg, depth in board.thread_order(msgs)]
    assert order == [(5, 0), (0, 0), (1, 1), (3, 2), (4, 1), (2, 0)]


def test_thread_order_cycle(script):
    board = script('board')
    msgs = [fake(0, 1), fake(1, 0), fake(2, 2)]
    order = sorted(msg.idx for msg, _ in board.thread_order(msgs))
    assert order == [0, 1, 2]


# list and reader


def test_lists_subscribed_public_messages(users, engine, script, session):
    session.session.user['msg_subscription'] = [u'bttf']
    root = post(u'biff', u'hoverboards')
    post(u'doc', u'Re: hoverboards', parent=root.idx)
    post(u'doc', u'off topic', tags=(u'other',))
    private(u'doc', u'marty', u'private matter')
    output = run_script(engine, script('board'), [u'q'])
    assert u'Board: all messages, 2 new' in output
    assert u'hoverboards' in output
    # the reply is indented, below its parent.
    assert u'  Re: hoverboards' in output
    assert output.index(u'hoverboards') < output.index(u'Re: hoverboards')
    assert u'off topic' not in output
    assert u'private matter' not in output


def test_read_marks_read(users, engine, script, session):
    msg = post(u'biff', u'hello', body=u'body text\r\n> a quote')
    output = run_script(engine, script('board'), [u'\r', PAUSE, u'q', u'q'])
    assert u'body text' in output
    assert u'Tags: bttf' in output
    assert msg.idx in session.session.user.get('readmsgs')


def test_new_only(users, engine, script, session):
    old = post(u'biff', u'old news')
    post(u'doc', u'fresh news')
    session.session.user['readmsgs'] = {old.idx}
    output = run_script(engine, script('board'), [u'q'], new_only=True)
    assert u'fresh news' in output
    assert u'old news' not in output


def test_mark_all_read(users, engine, script, session):
    msgs = [post(u'biff', u'one'), post(u'doc', u'two')]
    run_script(engine, script('board'), [u'm', u'y', u'q'])
    assert {msg.idx for msg in msgs} <= session.session.user.get('readmsgs')


def test_post(users, engine, script, monkeypatch):
    post(u'biff', u'existing', tags=(u'bttf',))
    board = script('board')
    gosub_editor(monkeypatch, board, u'my message')
    # To: all, subject, editor, then a new tag is added and accepted.
    output = run_script(engine, board, [
        u'p', u'\r', u'great scott\r', PAUSE, u'a', u'flux\r', u'\r', u'q'])
    assert u'Posted.' in output
    (idx,) = list_msgs(tags=(u'flux',))
    msg = get_msg(idx)
    assert (msg.author, msg.recipient, msg.subject, msg.body) == (
        u'marty', None, u'great scott', u'my message')
    assert msg.tags == {u'flux', u'public'}


def test_post_choose_existing_tag(users, engine, script, monkeypatch):
    post(u'biff', u'existing', tags=(u'bttf',))
    board = script('board')
    gosub_editor(monkeypatch, board, u'my message')
    run_script(engine, board, [
        u'p', u'doc\r', u'to doc\r', PAUSE, u' ', u'\r', u'q'])
    (idx,) = list_msgs(tags=(u'bttf',)) - {0}
    msg = get_msg(idx)
    assert msg.recipient == u'doc'
    assert msg.tags == {u'bttf', u'public'}


def test_post_canceled_at_tags_keeps_draft(users, engine, script,
                                           monkeypatch, session):
    board = script('board')
    gosub_editor(monkeypatch, board, u'keep me')
    output = run_script(engine, board, [
        u'p', u'\r', u'subject\r', PAUSE, ESC, PAUSE, u'q'])
    assert u'kept as a draft' in output
    assert session.session.user.get('msg_draft') == u'keep me'
    assert not DBProxy(MSGDB).keys()


def test_reply(users, engine, script, monkeypatch):
    root = post(u'biff', u'manure', tags=(u'bttf', u'farm'))
    board = script('board')
    calls = []
    gosub_editor(monkeypatch, board, u'I hate manure', record=calls)
    run_script(engine, board, [u'r', PAUSE, u'q'])
    assert calls[0]['quote'] == u'text'
    assert u'biff wrote:' in calls[0]['quote_header']
    (idx,) = list_msgs(tags=(u'farm',)) - {root.idx}
    reply = get_msg(idx)
    assert (reply.parent, reply.recipient, reply.subject) == (
        root.idx, u'biff', u'Re: manure')
    assert reply.tags == {u'bttf', u'farm', u'public'}


def test_mail_author(users, engine, script, monkeypatch):
    msg = post(u'biff', u'hello')
    board = script('board')
    calls = []
    monkeypatch.setattr(board, 'gosub',
                        lambda name, **kwargs: calls.append((name, kwargs)))
    run_script(engine, board, [u'\r', PAUSE, u'm', PAUSE, u'q', u'q'])
    assert calls == [('mail', {'write': True, 'recipient': u'biff',
                               'reply_to': msg.idx})]


def test_edit_tags_by_author(users, engine, script):
    msg = post(u'marty', u'mine', tags=(u'bttf',))
    post(u'biff', u'other', tags=(u'cars',))
    # 'mine' is listed second, below the newer thread.  Its tags listed:
    # bttf (chosen), cars.  Unchoose bttf, choose cars.
    run_script(engine, script('board'), [
        DOWN, u'\r', PAUSE, u't', PAUSE, u' ', u' ', u'\r', PAUSE, u'q',
        u'q'])
    assert get_msg(msg.idx).tags == {u'cars', u'public'}


def test_delete_only_by_sysop(users, engine, script, session):
    from x84.bbs.userbase import get_user
    msg = post(u'doc', u'spam')
    output = run_script(engine, script('board'), [
        u'\r', PAUSE, u'd', PAUSE, u'q', u'q'])
    assert u'D Delete' not in output
    assert get_msg(msg.idx)
    # biff, the first user, is sysop.
    session.session.user = get_user(u'biff')
    run_script(engine, script('board'), [
        u'\r', PAUSE, u'd', u'y', PAUSE, u'q'])
    assert str(msg.idx) not in DBProxy(MSGDB).keys()


def test_subscription(users, engine, script, session):
    post(u'biff', u'one', tags=(u'bttf',))
    post(u'biff', u'two', tags=(u'bttf',))
    post(u'biff', u'three', tags=(u'cars',))
    # listed: * (chosen), public, bttf, cars.  Choose bttf, unchoose *.
    output = run_script(engine, script('board'), [
        DOWN, DOWN, u' ', UP, UP, UP, u' ', u'\r'], subscribe=True)
    assert u'subscription tags' in output
    assert session.session.user['msg_subscription'] == [u'bttf']


def test_subscription_pattern(users, engine, script, session):
    post(u'biff', u'one', tags=(u'lang-python',))
    output = run_script(engine, script('board'), [
        u'a', u'nomatch-*\r', PAUSE, u'a', u'lang-*\r', DOWN, u' ',
        u'\r'], subscribe=True)
    assert u'No tags match nomatch-*' in output
    # lang-* is added, chosen, above '*', which is unchosen.
    assert session.session.user['msg_subscription'] == [u'lang-*']


def test_subscription_requires_a_tag(users, engine, script, session):
    post(u'biff', u'one')
    output = run_script(engine, script('board'), [
        u' ', u'\r', PAUSE, u'\r'], subscribe=True)
    assert u'Choose at least one tag' in output
    assert session.session.user['msg_subscription'] == [u'*']


def test_new_message_notice(users, engine, script):
    board = script('board')

    def deliver():
        time.sleep(0.5)
        msg = Msg(subject=u'breaking', body=u'b')
        msg.author, msg.idx, msg._stime = u'doc', 7, msg.ctime
        msg.tags = {u'public'}
        DBProxy(MSGDB, use_session=False)['7'] = msg
        with DBProxy('tags', use_session=False) as db_tag:
            db_tag[u'public'] = {7}
        engine.pipe.send(('global', ('newmsg', 7)))

    import threading
    thread = threading.Thread(target=deliver)
    thread.start()
    output = run_script(engine, board, [1.5, u'q'])
    thread.join()
    assert u'New message from doc.' in output
    assert u'breaking' in output


def test_draws_only_ascii(users, engine, script):
    post(u'biff', u'caf\xe9', body=u'|04red|07 na\xefve', tags=(u't\xe4g',))
    output = run_script(engine, script('board'), [
        u'\r', PAUSE, u'q', u'c', PAUSE, ESC, PAUSE, u'q'])
    for typed in (u'caf\xe9', u'na\xefve', u't\xe4g'):
        output = output.replace(typed, u'')
    assert all(ord(ucs) < 128 for ucs in output), [
        ucs for ucs in output if ord(ucs) >= 128]


def test_list_columns_colored(users, engine, script, session):
    board = script('board')
    unread = post(u'biff', u'one')
    read = post(u'doc', u'two')
    post(u'marty', u'three')
    session.session.user['readmsgs'] = {read.idx}
    output = run_script(engine, board, [u'q'])
    # 'three', the newest, is selected; of the others, the unread author is
    # bright, and three spaces part the author and subject columns.
    name = max(4, board.username_max_length)
    bold_cyan, cyan = u'\x1b[1m\x1b[36m', u'\x1b[36m'
    assert (bold_cyan + u'biff' + u' ' * (name - 4 + 3) + u'\x1b[m' +
            u'\x1b[1m\x1b[37mone') in output
    assert cyan + u'doc' + u' ' * (name - 3 + 3) + u'\x1b[m' in output
    assert u'From' + u' ' * (name - 4 + 3) + u'Subject' in output
    assert unread.idx not in session.session.user['readmsgs']


def test_subscription_of_another_user(users, engine, script, session):
    post(u'biff', u'one')
    run_script(engine, script('board'), [u'\r'], subscribe=True,
               handle=u'doc')
    from x84.bbs.userbase import get_user
    assert get_user(u'doc')['msg_subscription'] == [u'*']
    assert session.session.user.get('msg_subscription') is None


@pytest.mark.parametrize('kind', ['tn3270', 'tn3270-tls'])
def test_clicks_of_3270(users, engine, script, session, kind):
    # at a 3270, ENTER is a click of the mouse where the cursor is: a
    # message clicked is read, and hints of the bars are their keys.
    session.session.kind = kind
    # as answered by the tn3270 server, to queries of mouse modes, which
    # are supported and not enabled (see test_integration.py).
    session.session.terminal._dec_mode_cache.update({1000: 2, 1006: 2})
    post(u'biff', u'first', body=u'first body')
    post(u'doc', u'second', body=u'second body')
    hints = u' Enter Read P Post R Reply C Tags V By date M Mark read'
    quit_x = len(hints + u' Tab New ') + 2

    def click(row, col):
        return u'\x1b[<0;{0};{1}M\x1b[<0;{0};{1}m'.format(col + 1, row + 1)
    keys = [click(3, 10), PAUSE, u'q', PAUSE, click(23, quit_x)]
    output = run_script(engine, script('board'), keys)
    assert u'\x1b[?1006;1000h' in output.replace(u'\x1b[?1000;1006h',
                                                 u'\x1b[?1006;1000h')
    assert u'first body' in output
    assert u'second body' not in output


def test_page_of_3270(users, engine, script, session):
    # at a 3270, the title bar names the PF keys that page, and they are
    # pressed by clicking them, too: a page scrolls by a page.
    session.session.kind = 'tn3270'
    session.session.terminal._dec_mode_cache.update({1000: 2, 1006: 2})
    for num in range(30):
        post(u'biff', u'subject {0}'.format(num))
    title = u' Board: all messages, 30 new'
    right = u'PF7 Up PF8 Down Tab New '
    pf8_x = 79 - len(right) + right.index(u'PF8')
    click = u'\x1b[<0;{0};1M\x1b[<0;{0};1m'.format(pf8_x + 1)
    output = run_script(engine, script('board'), [click, PAUSE, u'q'])
    assert title in output and right in output
    # 21 rows of the list, at 24 rows: the 22nd message is selected.
    assert u'22/30' in output
