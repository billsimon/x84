""" Tests for the terminal type given to doors. """
# local
from x84.bbs import door


def entry(folder, *path):
    target = folder.joinpath(*path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b'')


def test_has_terminfo(tmp_path):
    entry(tmp_path, 's', 'syncterm')
    # as on macOS, by hexadecimal value of the first letter.
    entry(tmp_path, '61', 'ansi-bbs')
    env = {'TERMINFO': str(tmp_path)}
    assert door.has_terminfo('syncterm', env)
    assert door.has_terminfo('ansi-bbs', env)
    assert not door.has_terminfo('xterm-kitty', {'TERMINFO_DIRS': ''})
    for kind in ('', '../s/syncterm', 's/syncterm', '.hidden'):
        assert not door.has_terminfo(kind, env)


def test_has_terminfo_of_home(tmp_path, monkeypatch):
    monkeypatch.setattr(door, 'TERMINFO_DIRS', ())
    entry(tmp_path, '.terminfo', 's', 'syncterm')
    assert door.has_terminfo('syncterm', {'HOME': str(tmp_path)})
    assert not door.has_terminfo('syncterm', {})


def test_door_term(session, tmp_path, monkeypatch):
    monkeypatch.setattr(door, 'TERMINFO_DIRS', ())
    kind = session.session.terminal.kind
    env = {'HOME': str(tmp_path)}
    # unknown to this system: 'ansi' is given instead.
    assert door.Door('/bin/true', env=env).env['TERM'] == 'ansi'
    entry(tmp_path, '.terminfo', kind[0], kind)
    assert door.Door('/bin/true', env=env).env['TERM'] == kind
    # a type given by the script is kept.
    assert door.Door('/bin/true', env=dict(env, TERM='vt100')
                     ).env['TERM'] == 'vt100'
