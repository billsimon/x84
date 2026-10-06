""" Shared fixtures for x/84 tests. """
# std imports
import multiprocessing
import os

# 3rd-party
import pytest

# local
import x84.bbs.ini
import x84.bbs.session
import x84.bbs.userbase
import x84.db


@pytest.fixture
def cfg(tmp_path):
    """ Initialize bbs configuration with defaults, using a temporary datapath. """
    config = x84.bbs.ini.init_bbs_ini()
    config.set('system', 'datapath', str(tmp_path / 'data'))
    # bcrypt is deliberately slow; tests needn't be.
    config.set('system', 'password_digest', 'internal')
    saved = x84.bbs.ini.CFG
    x84.bbs.ini.CFG = config
    x84.bbs.userbase.FN_PASSWORD_DIGEST = None
    yield config
    x84.db.close_databases()
    x84.bbs.ini.CFG = saved
    x84.bbs.userbase.FN_PASSWORD_DIGEST = None


class SessionHarness(object):

    """ A :class:`x84.bbs.session.Session`, with the engine side of its pipes. """

    def __init__(self, session, master_write, master_read):
        self.session = session
        self.master_write = master_write
        self.master_read = master_read

    @property
    def term(self):
        return self.session.terminal

    def send(self, event, data):
        """ Send event to session, as the engine would. """
        self.master_write.send((event, data))

    def keys(self, text):
        """ Send keyboard input to session. """
        self.send('input', text.encode('utf8'))

    def output(self):
        """ Return all output written by session so far. """
        result = []
        while self.master_read.poll():
            event, data = self.master_read.recv()
            if event == 'output':
                result.append(data[0])
        return u''.join(result)


@pytest.fixture
def session(cfg):
    """ Return a :class:`SessionHarness` of a session in this process. """
    from x84.bbs.ipc import IPCStream
    from x84.terminal import Terminal

    child_read, master_write = multiprocessing.Pipe(duplex=False)
    master_read, child_write = multiprocessing.Pipe(duplex=False)
    term = Terminal(kind='xterm-256color', stream=IPCStream(child_write),
                    rows=24, columns=80)
    env = {'TERM': 'xterm-256color', 'encoding': 'utf8'}
    sess = x84.bbs.session.Session(
        terminal=term, sid='test-127.0.0.1:1', env=env,
        child_pipes=(child_write, child_read), kind='test',
        addrport='127.0.0.1:1', matrix_args=(), matrix_kwargs={})
    yield SessionHarness(sess, master_write, master_read)
    x84.bbs.session.SESSION = None
    for conn in (child_read, master_write, master_read, child_write):
        conn.close()


@pytest.fixture
def free_port():
    """ Return a function that returns an unused tcp port. """
    import socket

    def _free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]
    return _free_port


def pytest_configure(config):
    # sessions are never started by fork in x84; make test runs consistent.
    os.environ.setdefault('PYTHONHASHSEED', '0')
