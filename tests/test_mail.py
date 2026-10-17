""" Tests for private mail: the message base indices, and the mail script. """
# std imports
import threading
import time

# local
from x84.bbs.dbproxy import DBProxy
from x84.bbs.msgbase import (
    MSGDB,
    PRIVDB,
    SENTDB,
    Msg,
    get_msg,
    list_privmsgs,
    list_sent,
    remove_privmsg,
    remove_sent,
)
from tests.screens import (  # noqa: F401 (fixtures)
    PAUSE,
    engine,
    gosub_editor,
    run_script as run_mail,
    script,
    users,
)

def mail(author, recipient, subject=u'hi', body=u'text', parent=None):
    msg = Msg(recipient=recipient, subject=subject, body=body)
    msg.author = author
    msg.parent = parent
    msg.save(send_net=False)
    return msg


# message base


def test_mail_indexed_by_recipient_and_author(cfg):
    msg = mail(u'biff', u'marty')
    assert list_privmsgs(u'marty') == {msg.idx}
    assert list_sent(u'biff') == {msg.idx}
    assert list_sent(u'marty') == set()


def test_public_message_not_indexed(cfg):
    msg = Msg(subject=u'hi', body=u'all')
    msg.author, msg.tags = u'biff', {u'public'}
    msg.save(send_net=False)
    assert list_privmsgs() == set()
    assert list_sent(u'biff') == set()


def test_message_without_recipient_not_indexed(cfg):
    # earlier versions indexed a NULL key, hiding the message from all.
    msg = Msg(subject=u'hi', body=u'all')
    msg.author = u'biff'
    msg.save(send_net=False)
    assert list_privmsgs() == set()


def test_remove_is_per_folder(cfg):
    msg = mail(u'biff', u'marty')
    remove_privmsg(u'marty', msg.idx)
    assert list_privmsgs(u'marty') == set()
    assert list_sent(u'biff') == {msg.idx}
    remove_sent(u'biff', msg.idx)
    assert list_sent(u'biff') == set()
    # the message remains, as earlier versions kept it.
    assert get_msg(msg.idx).body == u'text'


def test_removed_mail_not_restored_when_replied(cfg):
    msg = mail(u'biff', u'marty')
    remove_privmsg(u'marty', msg.idx)
    remove_sent(u'biff', msg.idx)
    reply = mail(u'marty', u'biff', parent=msg.idx)
    assert get_msg(msg.idx).children == {reply.idx}
    assert list_privmsgs(u'marty') == set()
    assert list_sent(u'biff') == set()
    assert list_privmsgs(u'biff') == {reply.idx}


def test_sent_indexed_from_existing_mail(cfg):
    # mail saved by an earlier version, of index 'privmsg' only.
    old = []
    for idx, (author, recipient, tags) in enumerate((
            (u'biff', u'marty', set()),
            (u'biff', u'doc', {u'bttf'}),
            (u'marty', u'biff', set()),
            (u'doc', None, {u'hi'}),
            (u'doc', None, {u'public'}))):
        msg = Msg(recipient=recipient, subject=u's', body=u'b')
        msg.author, msg.tags, msg.idx = author, tags, idx
        msg._stime = msg.ctime
        old.append(msg)
    with DBProxy(MSGDB) as db_msg:
        for msg in old:
            db_msg[str(msg.idx)] = msg
    with DBProxy(PRIVDB) as db_priv:
        db_priv[u'marty'] = {0}
        db_priv[u'doc'] = {1}
        db_priv[u'biff'] = {2}

    assert list_sent(u'biff') == {0, 1}
    assert list_sent(u'marty') == {2}
    assert list_sent(u'doc') == set()
    # indexed only once: removal is kept.
    remove_sent(u'biff', 0)
    assert list_sent(u'biff') == {1}
    assert DBProxy(SENTDB, table='status')['indexed'] is True


# mail script


def test_inbox_lists_mail(users, engine, script):
    mail(u'biff', u'marty', subject=u'hoverboard')
    mail(u'doc', u'marty', subject=u'flux capacitor')
    mail(u'biff', u'doc', subject=u'not for marty')
    output = run_mail(engine, script('mail'), [u'q'])
    assert u'Inbox, 2 new' in output
    assert u'hoverboard' in output
    assert u'flux capacitor' in output
    assert u'not for marty' not in output


def test_read_marks_read(users, engine, script, session):
    msg = mail(u'biff', u'marty', subject=u'hello', body=u'line one\r\n'
               u'> quoted\r\nline three')
    output = run_mail(engine, script('mail'), [u'\r', u'q', u'q'])
    assert u'line one' in output
    assert u'> quoted' in output
    assert u'From: biff' in output
    assert msg.idx in session.session.user.get('readmsgs', set())


def test_sent_folder(users, engine, script):
    mail(u'marty', u'doc', subject=u'to doc')
    mail(u'biff', u'marty', subject=u'from biff')
    output = run_mail(engine, script('mail'), [u'\t', PAUSE, u'q'])
    # only the changed part of the title, 'Inbox', is redrawn.
    assert u'Sent' in output.split(u'from biff', 1)[1]
    assert u'to doc' in output


def test_delete_from_inbox(users, engine, script):
    msg = mail(u'biff', u'marty', subject=u'delete me')
    run_mail(engine, script('mail'), [u'd', u'y', u'q'])
    assert list_privmsgs(u'marty') == set()
    assert list_sent(u'biff') == {msg.idx}


def test_delete_declined(users, engine, script):
    msg = mail(u'biff', u'marty', subject=u'keep me')
    run_mail(engine, script('mail'), [u'd', u'n', u'q'])
    assert list_privmsgs(u'marty') == {msg.idx}


def test_write_mail(users, engine, script, monkeypatch):
    mail_script = script('mail')
    gosub_editor(monkeypatch, mail_script, u'great scott')
    output = run_mail(engine, mail_script,
                      [u'w', u'doc\r', u'1.21 gigawatts\r', u'q'])
    assert u'Sent to doc.' in output
    (idx,) = list_privmsgs(u'doc')
    msg = get_msg(idx)
    assert (msg.author, msg.subject, msg.body) == (
        u'marty', u'1.21 gigawatts', u'great scott')
    assert msg.tags == set()
    assert list_sent(u'marty') == {idx}
    assert ('newmsg', idx) in engine.broadcasts


def test_write_recipient_case_and_close_match(users, engine, script,
                                              monkeypatch):
    mail_script = script('mail')
    gosub_editor(monkeypatch, mail_script, u'body')
    # 'DOC' is matched exactly, 'bif' by confirmation.
    run_mail(engine, mail_script, [u'w', u'DOC\r', u's\r', u'w', u'bif\r',
                                   u'y', u's\r', u'q'])
    assert len(list_privmsgs(u'doc')) == 1
    assert len(list_privmsgs(u'biff')) == 1


def test_write_canceled(users, engine, script, monkeypatch):
    mail_script = script('mail')
    gosub_editor(monkeypatch, mail_script, False)
    output = run_mail(engine, mail_script, [u'w', u'doc\r', u'subj\r', u'q'])
    assert u'Not sent.' in output
    assert list_privmsgs(u'doc') == set()


def test_reply_quotes_and_marks_replied(users, engine, script, monkeypatch,
                                        session):
    mail_script = script('mail')
    calls = []
    gosub_editor(monkeypatch, mail_script, u'no way', record=calls)
    msg = mail(u'biff', u'marty', subject=u'make like a tree')
    # read, reply, and return to the list.
    output = run_mail(engine, mail_script,
                      [u'\r', PAUSE, u'r', PAUSE, u'q', PAUSE, u'q'])
    assert calls[0]['quote'] == u'text'
    assert u'biff wrote:' in calls[0]['quote_header']
    assert calls[0]['title'] == u'Re: make like a tree  To: biff'
    (idx,) = list_privmsgs(u'biff')
    reply = get_msg(idx)
    assert reply.parent == msg.idx
    assert reply.subject == u'Re: make like a tree'
    assert msg.idx in session.session.user.get('mail_replied')
    # flagged replied in the list.
    assert u'R biff' in output


def test_reply_to_sent_goes_to_recipient(users, engine, script, monkeypatch):
    mail_script = script('mail')
    gosub_editor(monkeypatch, mail_script, u'also')
    mail(u'marty', u'doc', subject=u'Re: plutonium')
    run_mail(engine, mail_script, [u's', PAUSE, u'r', PAUSE, u'q'])
    (idx,) = list_privmsgs(u'doc') - {0}
    assert get_msg(idx).subject == u'Re: plutonium'


def test_forward(users, engine, script, monkeypatch):
    mail_script = script('mail')
    calls = []
    gosub_editor(monkeypatch, mail_script, u'see below', record=calls)
    mail(u'biff', u'marty', subject=u'manure', body=u'I hate manure')
    run_mail(engine, mail_script, [u'f', u'doc\r', u'\r', u'q'])
    assert u'I hate manure' in calls[0]['continue_draft']
    assert u'From: biff' in calls[0]['continue_draft']
    assert calls[0]['quote'] is None
    (idx,) = list_privmsgs(u'doc')
    msg = get_msg(idx)
    assert msg.subject == u'Fwd: manure'
    assert msg.parent is None


def test_new_mail_notice(users, engine, script):
    mail_script = script('mail')

    def deliver():
        # as by another session: saved, and broadcast by the engine.
        time.sleep(0.5)
        msg = Msg(recipient=u'marty', subject=u'1955', body=u'b')
        msg.author, msg.idx, msg._stime = u'doc', 99, msg.ctime
        DBProxy(MSGDB, use_session=False)['99'] = msg
        with DBProxy(PRIVDB, use_session=False) as db_priv:
            db_priv[u'marty'] = db_priv.get(u'marty', set()) | {99}
        engine.pipe.send(('global', ('newmsg', 99)))

    thread = threading.Thread(target=deliver)
    thread.start()
    output = run_mail(engine, mail_script, [1.5, u'q'])
    thread.join()
    assert u'New mail from doc.' in output
    assert u'1955' in output


def test_pipe_colors_and_ascii(users, engine, script):
    # colors of pipe codes are displayed; all else drawn is ascii.
    mail(u'biff', u'marty', subject=u'na\xefve', body=u'|04red|07 and caf\xe9')
    output = run_mail(engine, script('mail'), [u'\r', PAUSE, u'q', u'q'])
    assert u'\x1b[' in output and u'red' in output
    for typed in (u'na\xefve', u'caf\xe9'):
        output = output.replace(typed, u'')
    assert all(ord(ucs) < 128 for ucs in output), [
        ucs for ucs in output if ord(ucs) >= 128]
