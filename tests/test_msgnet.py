""" Tests for the intra-bbs message network (x84net) of x/84. """
# 3rd-party
import pytest

pytest.importorskip('web')


@pytest.fixture
def network(cfg):
    cfg.set('msg', 'server_tags', 'testnet')
    return 'testnet'


def make_msgs(count, tags):
    from x84.bbs import Msg
    msgs = []
    for idx in range(count):
        msg = Msg(subject=u'subject {0}'.format(idx), body=u'body')
        msg.author = u'author'
        msg.tags = set(tags)
        msg.save(send_net=False)
        msgs.append(msg)
    return msgs


def test_serve_messages_in_order_with_batch_limit(network):
    from x84.bbs import DBProxy
    from x84.webmodules import msgserve
    make_msgs(msgserve.BATCH_MSGS + 5, (u'public', network))
    db_source = DBProxy('testnetsource', use_session=False)

    # clients request messages following id -1 initially: message 0 must be
    # included, and the first batch must be the oldest messages.
    first = msgserve.serve_messages_for(
        board_id='1', request_data={'network': network, 'last': -1},
        db_source=db_source)['messages']
    assert [msg['id'] for msg in first] == list(range(msgserve.BATCH_MSGS))

    rest = msgserve.serve_messages_for(
        board_id='1', request_data={'network': network,
                                    'last': first[-1]['id']},
        db_source=db_source)['messages']
    assert [msg['id'] for msg in rest] == list(
        range(msgserve.BATCH_MSGS, msgserve.BATCH_MSGS + 5))
    assert all(network not in msg['tags'] for msg in rest)


def test_serve_excludes_messages_of_requesting_board(network):
    from x84.bbs import DBProxy
    from x84.webmodules import msgserve
    msgs = make_msgs(2, (u'public', network))
    db_source = DBProxy('testnetsource', use_session=False)
    db_source[msgs[0].idx] = '1'
    served = msgserve.serve_messages_for(
        board_id='1', request_data={'network': network, 'last': -1},
        db_source=db_source)['messages']
    assert [msg['id'] for msg in served] == [msgs[1].idx]


def test_parse_auth_rejects_future_and_stale_tokens():
    import time
    from x84.webmodules import msgserve
    now = int(time.time())
    assert msgserve.parse_auth({'auth': '1|token|{0}'.format(now)})
    for when in (now + 3600, now - 3600):
        with pytest.raises(ValueError):
            msgserve.parse_auth({'auth': '1|token|{0}'.format(when)})


def test_token_matches_server_computation(network):
    import hashlib
    from x84 import msgpoll
    net = {'token': 'secret', 'board_id': '7'}
    board_id, digest, when = msgpoll.get_token(net).split('|')
    assert board_id == '7'
    assert digest == hashlib.sha256(
        '{0}{1}'.format('secret', when).encode('utf8')).hexdigest()


def test_poll_stores_in_order_and_discards_duplicates(cfg, tmp_path,
                                                      monkeypatch):
    from x84 import msgpoll
    from x84.bbs import list_msgs, get_msg
    remote = [
        {'id': 12, 'author': u'b', 'recipient': None, 'subject': u'second',
         'body': u'2', 'tags': [u'public'], 'parent': None,
         'ctime': '2026-01-01 00:00:02'},
        {'id': 11, 'author': u'a', 'recipient': None, 'subject': u'first',
         'body': u'1', 'tags': [u'public'], 'parent': None,
         'ctime': '2026-01-01 00:00:01'},
    ]
    monkeypatch.setattr(msgpoll, 'pull_rest',
                        lambda net, last_msg_id: list(remote))
    net = {'name': 'testnet',
           'last_file': str(tmp_path / 'no-such-folder' / 'testnet_last')}
    msgpoll.poll_network_for_messages(net)
    assert [get_msg(idx).subject for idx in sorted(list_msgs())] == [
        u'first', u'second']
    assert all(u'testnet' in get_msg(idx).tags for idx in list_msgs())
    assert open(net['last_file']).read() == '12'

    # the same messages, received again, are discarded.
    msgpoll.poll_network_for_messages(net)
    assert len(list_msgs()) == 2
