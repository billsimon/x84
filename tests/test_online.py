""" Tests of the who's online script of the default board. """
# std imports
import logging


class Client(object):

    """ A client of a session, of nothing waiting to be sent. """

    SEND_HIGH_WATER = 1
    send_buffer = b''


class Pipe(object):

    """ The pipe of a session that sends nothing. """

    def poll(self):
        return False


def test_disconnect(script, session, monkeypatch):
    # the sysop's [d]isconnect of a node kills its session, by the engine.
    import x84.engine
    from x84.terminal import TerminalProcess
    online = script('online')
    target = {'sid': 'telnet-10.0.0.2:5000', 'handle': u'biff'}
    monkeypatch.setattr(online, 'get_node', lambda sessions: (2, target))
    killed = []
    monkeypatch.setattr(x84.engine, 'kill_session',
                        lambda client, reason: killed.append((client, reason)))
    terminals = []
    for sid, pipe in ((session.session.sid, session.master_read),
                      (target['sid'], Pipe())):
        tty = TerminalProcess.__new__(TerminalProcess)
        tty.client, tty.sid, tty.master_read = Client(), sid, pipe
        terminals.append((sid, tty))

    assert online.disconnect({})
    x84.engine.session_recv(terminals, logging.getLogger('test'), False)
    assert killed == [(terminals[1][1].client, 'remote-disconnect by {0}'
                       .format(session.session.sid))]


def test_sendmsg_writes_mail(script, session, engine, users, monkeypatch):
    # [s]end msg writes private mail to the user of the node chosen.
    import x84.bbs
    online = script('online')
    target = {'sid': 'telnet-10.0.0.2:5000', 'handle': u'BIFF'}
    monkeypatch.setattr(online, 'get_node', lambda sessions: (1, target))
    calls = []
    monkeypatch.setattr(x84.bbs, 'gosub',
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    assert online.sendmsg({})
    assert calls == [(('mail',), {'write': True, 'recipient': u'biff'})]


def test_sendmsg_not_a_user(script, session, engine, users, monkeypatch):
    # a caller at the login prompt is not sent mail.
    import x84.bbs
    online = script('online')
    target = {'sid': 'telnet-10.0.0.2:5000', 'handle': u'anonymous'}
    monkeypatch.setattr(online, 'get_node', lambda sessions: (1, target))
    calls = []
    monkeypatch.setattr(x84.bbs, 'gosub',
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    assert not online.sendmsg({})
    assert calls == []
