""" Tests for the web modules of x/84. """
# std imports
import json

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
