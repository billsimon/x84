""" Tests of the menus of the default board, of common.py. """
# std imports
import collections
import threading
import time

# 3rd-party
import pytest

pyte = pytest.importorskip('pyte')

MenuItem = collections.namedtuple('MenuItem', ['inp_key', 'text'])

ITEMS = [MenuItem(u'irc', u'irc chat'), MenuItem(u'who', u"who's online"),
         MenuItem(u'pe', u'profile editor'), MenuItem(u'weather',
                                                      u'weather forecast'),
         MenuItem(u'g', u'logoff system')]


def test_menu_entries_located(script, session):
    # each menu item is where it is displayed: its key in brackets begins
    # it, and its text ends it.
    common = script('common')
    term = session.term
    colors = {'highlight': term.bold_magenta, 'lowlight': term.bold_black}
    output, entries = common.layout_menu_entries(term, 10, ITEMS, colors)
    assert output == common.render_menu_entries(term, 10, ITEMS, colors)
    screen = pyte.Screen(term.width, term.height)
    pyte.Stream(screen).feed(u'\x1b[12;1H' + output)
    assert [item for _, _, _, item in entries] == ITEMS
    for row, column, width, item in entries:
        shown = screen.display[11 + row][column:column + width]
        assert shown == common.decorate_menu_item(
            item, {'highlight': str, 'lowlight': str}), shown


def test_menu_entries_located_only_at_3270(script, session):
    common = script('common')
    assert common.locate_menu_entries(session.term, 10, ITEMS) == []


class Chosen(Exception):

    """ Raised to end the main menu, once its scripts were chosen. """


def test_label_declared_only_at_3270(script, session):
    common = script('common')
    assert common.declare_label(u'Message') == u''
    session.session.env['TN3270_TOKEN'] = u'tok'
    assert common.declare_label(u'Message') == (
        u'\x1b]3270;tok;label;Message\x07')
    assert common.declare_label() == u'\x1b]3270;tok;label\x07'


def test_main_menu_type_ahead(users, engine, script, session, monkeypatch):
    # two commands typed at once: the second is chosen as soon as the
    # script of the first returns, without awaiting another key.
    main = script('main')
    scripts = dict((item.inp_key, item.script)
                   for item in main.get_menu_items(session.session))
    chosen = []

    def fake_gosub(name, *args, **kwargs):
        chosen.append((name, time.time()))
        if len(chosen) == 2:
            raise Chosen()
    monkeypatch.setattr(main, 'gosub', fake_gosub)

    done = threading.Event()

    def send():
        time.sleep(0.5)
        engine.keys(u'si\rlc\r')
        # another key, too late, should the second await one.
        if not done.wait(3):
            engine.keys(u'x')
    sender = threading.Thread(target=send)
    sender.start()
    stime = time.time()
    try:
        with pytest.raises(Chosen):
            main.main()
    finally:
        done.set()
        sender.join()
    assert [name for name, _ in chosen] == [scripts[u'si'], scripts[u'lc']]
    assert chosen[1][1] - stime < 3
