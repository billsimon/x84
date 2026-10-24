""" Tests for the web modules of x/84. """
# std imports
import json
import time

# 3rd-party
import pytest

pytest.importorskip('web')


def make_app(module):
    """ Return a web.py application serving only the given web module. """
    import web
    api = module.web_module()
    return web.application(api['urls'], api['funcs'], autoreload=False)


def test_url_keywords_do_not_replace_server_globals(cfg, monkeypatch):
    """ A web module's url keyword such as 'server' is not a global. """
    import sys
    import types
    from x84 import webserve

    class Handler(object):
        pass

    module = types.ModuleType('webmodules.clash')
    module.web_module = lambda: {'urls': ('/clash', 'server'),
                                 'funcs': {'server': Handler}}
    monkeypatch.setitem(sys.modules, 'webmodules.clash', module)
    monkeypatch.setitem(sys.modules, 'webmodules',
                        types.ModuleType('webmodules'))
    sys.modules['webmodules'].clash = module
    server = webserve.server
    urls, funcs = webserve.get_urls_funcs(['clash'])
    assert funcs['server'] is Handler
    assert webserve.server is server
    assert urls == ('/favicon.ico', 'favicon', '/clash', 'server')


def test_oneliners_zero_lists_none(cfg):
    from x84.bbs import DBProxy
    from x84.default.webmodules import oneliners
    DBProxy('oneliner', use_session=False)['0'] = {
        'alias': u'biff', 'oneliner': u'butthead',
        'timestamp': '2015-10-21 16:29:00'}
    app = make_app(oneliners)
    assert len(json.loads(app.request('/oneliners/1?json').data)) == 1
    assert json.loads(app.request('/oneliners/0?json').data) == []


class FakeClient(object):

    """ A connected client, as far as the ``online`` web module sees one. """

    def __init__(self, idle, duration):
        self._idle, self._duration = idle, duration

    def idle(self):
        return self._idle

    def duration(self):
        return self._duration


class FakeTTY(object):

    def __init__(self, info, idle=0, duration=0):
        self.info = info
        self.client = FakeClient(idle, duration)


@pytest.fixture
def terminals(monkeypatch):
    """ Return the registry of terminals, emptied for the test. """
    import x84.terminal
    registry = dict()
    monkeypatch.setattr(x84.terminal, 'TERMINALS', registry)
    return registry


def test_session_reports_handle_and_activity(session):
    from x84.bbs.userbase import User
    sess = session.session
    sess.activity = u'reading mail'
    sess.user = User(u'biff')
    reports = []
    while session.master_read.poll():
        event, data = session.master_read.recv()
        if event == 'session-info':
            reports.append(data)
    # no user is reported until one is assigned, such as by login.
    assert reports == [
        {'handle': None, 'activity': u'reading mail'},
        {'handle': u'biff', 'activity': u'reading mail'},
    ]


def test_online_lists_users_logged_in(cfg, terminals):
    from x84.default.webmodules import online
    terminals['telnet-1'] = FakeTTY({'handle': u'marty', 'activity': u'chat'},
                                    idle=5, duration=60)
    terminals['telnet-2'] = FakeTTY({'handle': u'doc', 'activity': u'mail'},
                                    idle=30, duration=600)
    # not yet logged in.
    terminals['telnet-3'] = FakeTTY({'handle': None, 'activity': u'login'})
    terminals['telnet-4'] = FakeTTY({})

    resp = make_app(online).request('/online?json')
    assert json.loads(resp.data) == [
        {'handle': u'doc', 'activity': u'mail', 'idle': 30, 'duration': 600},
        {'handle': u'marty', 'activity': u'chat', 'idle': 5, 'duration': 60},
    ]

    text = make_app(online).request('/online?text').data.decode('utf8')
    assert text.splitlines()[1:] == [
        ' --> doc: mail (0m30s idle)',
        ' --> marty: chat (0m 5s idle)',
    ]


def test_online_escapes_html(cfg, terminals):
    from x84.default.webmodules import online
    terminals['telnet-1'] = FakeTTY({'handle': u'<b>', 'activity': u'a&b'})
    page = make_app(online).request('/online/').data.decode('utf8')
    assert '<b>&lt;b&gt;:</b> a&amp;b' in page
    assert '<!DOCTYPE html>' in page
    embed = make_app(online).request('/online?embed').data.decode('utf8')
    assert '<!DOCTYPE html>' not in embed
    assert '&lt;b&gt;' in embed


def test_automsg_lists_last_goodbye_messages(cfg):
    from x84.bbs import DBProxy
    from x84.default.webmodules import automsg
    now = time.time()
    db = DBProxy('automsg', use_session=False)
    for idx in range(12):
        db['%d' % (idx,)] = (now - 3 * 3600, u'user%d' % (idx,), u'bye %d' % idx)

    # oldest first, as shown by logoff; '10' and '11' sort after '9'.
    resp = make_app(automsg).request('/automsg/3?json')
    assert [entry[1][1] for entry in json.loads(resp.data)] == [
        u'user9', u'user10', u'user11']
    assert len(json.loads(make_app(automsg).request('/automsg?json').data)
               ) == 10
    assert json.loads(make_app(automsg).request('/automsg/0?json').data
                      ) == []

    text = make_app(automsg).request('/automsg/1?text').data.decode('utf8')
    assert text.splitlines()[1:] == [' --> user11: bye 11 (3h 0m ago)']


def test_automsg_escapes_html(cfg):
    from x84.bbs import DBProxy
    from x84.default.webmodules import automsg
    DBProxy('automsg', use_session=False)['0'] = (
        time.time(), u'a<b', u'</li><script>')
    page = make_app(automsg).request('/automsg').data.decode('utf8')
    assert '<b>a&lt;b:</b> &lt;/li&gt;&lt;script&gt;' in page
