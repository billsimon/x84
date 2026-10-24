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

# fixtures of tests of full-screen scripts, for any test.
from tests.screens import engine, script, users  # noqa: F401


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


@pytest.fixture(scope='session')
def tls_cert(tmp_path_factory):
    """ Return paths of a self-signed certificate of 127.0.0.1, and its key. """
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u'127.0.0.1')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(u'127.0.0.1'))]),
                critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .sign(key, hashes.SHA256()))
    folder = tmp_path_factory.mktemp('tls')
    cert_path, key_path = folder / 'cert.pem', folder / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def pytest_configure(config):
    # sessions are never started by fork in x84; make test runs consistent.
    os.environ.setdefault('PYTHONHASHSEED', '0')
