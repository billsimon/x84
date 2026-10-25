""" Tests for the default board's chat script. """
# 3rd-party
import pytest

# local
from tests.screens import run_script


def record_routes(session, monkeypatch):
    """ Return a list of events the session routes to other sessions. """
    routes = []
    send_event = session.session.send_event

    def fake_send_event(event, data):
        if event == 'route':
            routes.append(data)
        return send_event(event, data)
    monkeypatch.setattr(session.session, 'send_event', fake_send_event)
    return routes


@pytest.mark.parametrize('kind', ['telnet', 'ssh', 'rlogin', 'modem',
                                  'tn3270'])
def test_chat_request_answered(script, session, engine, users, monkeypatch,
                               kind):
    # a chat request reaches a caller on any terminal, who may
    # decline it.
    session.session.kind = kind
    routes = record_routes(session, monkeypatch)
    chat = script('chat')
    output = run_script(engine, chat, [u'n'], call_from=u'biff',
                        other_sid='telnet-10.0.0.2:5000')
    assert u'accept chat from biff' in output
    assert routes == [('telnet-10.0.0.2:5000', 'chat', chat.HANGUP)]


def test_chat_request_rejected_by_sftp(script, session, engine, users,
                                       monkeypatch):
    # an sftp session has no terminal to chat at.
    session.session.kind = 'sftp'
    routes = record_routes(session, monkeypatch)
    chat = script('chat')
    assert chat.main(call_from=u'biff', other_sid='telnet-10.0.0.2:5000')
    assert routes == [('telnet-10.0.0.2:5000', 'chat', chat.REJECTED)]


def test_chat_dialing_names_the_other_user(script, session, engine, users,
                                           monkeypatch):
    # the dialing session's activity names the user it dials.
    routes = record_routes(session, monkeypatch)
    chat = script('chat')
    run_script(engine, chat, [u'q'], dial=u'biff',
               other_sid='telnet-10.0.0.2:5000')
    assert session.session.activity == u'requesting chat with biff'
    # the other session is sent to chat, then told about the hangup.
    assert routes[0][:2] == ('telnet-10.0.0.2:5000', 'gosub')
    assert routes[-1] == ('telnet-10.0.0.2:5000', 'chat', chat.HANGUP)
