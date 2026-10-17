""" Tests for the user profile editor script. """
# local
from x84.bbs.msgbase import Msg
from x84.bbs.userbase import get_user
from tests.screens import (  # noqa: F401 (fixtures)
    engine,
    run_script,
    script,
    users,
)


def save(author, recipient=None, public=True):
    msg = Msg(recipient=recipient, subject=u'subject', body=u'text')
    msg.author = author
    msg.tags = {u'public'} if public else set()
    msg.save(send_net=False)


def test_counts_posts_and_mail(users, engine, script):
    save(u'marty')
    save(u'marty', recipient=u'doc')
    save(u'marty', recipient=u'doc', public=False)
    save(u'biff')
    output = run_script(engine, script('profile'), [u'q'])
    assert u'2 posts, 1 mail sent' in output


def test_charset_cycles_and_applies(users, engine, script, session):
    profile = script('profile')
    user = session.session.user
    run_script(engine, profile, [u'c', u'q'])
    assert user.get('charset') == u'utf8'
    assert session.session.encoding == 'utf8'
    run_script(engine, profile, [u'c', u'q'])
    assert user.get('charset') == u'cp437'
    assert session.session.encoding == 'cp437'
    # 'auto' is detected at the next login, and the session keeps its own.
    output = run_script(engine, profile, [u'c', u'q'])
    assert user.get('charset') is None
    assert session.session.encoding == 'cp437'
    assert u'auto' in output


def test_charset_applied_at_login(users, engine, script, session):
    top = script('top')
    get_user(u'marty')['charset'] = u'cp437'
    top.login(session.session, get_user(u'marty'))
    assert session.session.encoding == 'cp437'


def test_toggle_units_and_expert(users, engine, script, session):
    output = run_script(engine, script('profile'), [u'u', u'x', u'q'])
    user = session.session.user
    assert user.get('centigrade') is True
    assert user.get('expert') is True
    assert u'yes' in output


def test_groups_read_only_for_users(users, engine, script, session):
    output = run_script(engine, script('profile'), [u'g', u'q'])
    assert u'groups' in output
    assert u'Groups this user is a member of' not in output


def test_tags_and_weather_of_another_user(users, engine, script, session,
                                          monkeypatch):
    profile = script('profile')
    calls = []
    monkeypatch.setattr(profile, 'gosub',
                        lambda name, **kwargs: calls.append((name, kwargs)))
    # biff, the first user, is sysop; the weather script saves only the
    # location of the session user.
    session.session.user = get_user(u'biff')
    run_script(engine, profile, [u't', u'w', u'q'], handle=u'marty')
    assert calls == [('board', {'subscribe': True, 'handle': u'marty'})]
    session.session.user = get_user(u'marty')
    run_script(engine, profile, [u'w', u'q'])
    assert calls[1:] == [('weather', {})]


def test_shows_weather_location_and_tags(users, engine, script, session):
    user = session.session.user
    user['location'] = {u'city': u'Hill Valley', u'state': u'CA'}
    user['msg_subscription'] = {u'bttf', u'cars'}
    output = run_script(engine, script('profile'), [u'q'])
    # names longer than the field keep their start.
    assert u'Hill Valley,..' in output
    assert u'bttf, cars' in output
