"""
Integration tests: a real x/84 server, driven by telnet and ssh clients.

Each test module run starts one server, in a subprocess, on free ports with
a temporary configuration and database.
"""
# std imports
import re
import signal
import subprocess
import sys
import time

# 3rd-party
import pytest

# local
from x84.bbs.telnet import TelnetClient

#: strips terminal sequences from output, for matching text on screen.
RE_SEQUENCE = re.compile(
    r'\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][0-9A-Za-z]|%[@G]|[=>78])')


class Screen(object):

    """ Telnet client accumulating text displayed by the server. """

    def __init__(self, port):
        self.client = TelnetClient(term_type='xterm-256color',
                                   width=80, height=25)
        self.client.open('127.0.0.1', port)
        self.raw = u''
        self.eof = False

    @property
    def text(self):
        return RE_SEQUENCE.sub(u'', self.raw)

    def read(self, duration):
        stime = time.time()
        while time.time() - stime < duration and not self.eof:
            try:
                data = self.client.read_available(timeout=0.05)
            except EOFError:
                self.eof = True
                break
            self.raw += data.decode('utf8', 'replace')

    def expect(self, pattern, timeout=20):
        stime = time.time()
        while time.time() - stime < timeout:
            if re.search(pattern, self.text):
                return
            if self.eof:
                break
            self.read(0.1)
        raise AssertionError('{0!r} not on screen:\n{1}'.format(
            pattern, self.text[-2000:]))

    def send(self, text):
        self.client.write(text.encode('utf8'))
        time.sleep(0.2)

    def close(self):
        self.client.close()


@pytest.fixture(scope='module')
def server(tmp_path_factory, request):
    """ Start x84 server on free ports, return dict of ports and paths. """
    import socket
    import x84.bbs.ini

    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    root = tmp_path_factory.mktemp('x84')
    ports = {'telnet': free_port(), 'ssh': free_port()}
    cfg = x84.bbs.ini.init_bbs_ini()
    cfg.set('system', 'datapath', str(root / 'data'))
    cfg.set('system', 'password_digest', 'internal')
    cfg.set('telnet', 'port', str(ports['telnet']))
    cfg.set('ssh', 'enabled', 'yes')
    cfg.set('ssh', 'port', str(ports['ssh']))
    cfg.set('ssh', 'hostkey', str(root / 'ssh_host_rsa_key'))
    cfg.set('ssh', 'hostkeybits', '2048')
    with open(root / 'default.ini', 'w') as fout:
        cfg.write(fout)
    log = x84.bbs.ini.init_log_ini(log_folder=str(root))
    with open(root / 'logging.ini', 'w') as fout:
        log.write(fout)

    output = open(root / 'output.log', 'w')
    proc = subprocess.Popen(
        [sys.executable, '-m', 'x84.engine',
         '--config', str(root / 'default.ini'),
         '--logger', str(root / 'logging.ini')],
        stdout=output, stderr=subprocess.STDOUT, cwd=str(root))

    def server_log():
        output.flush()
        return (root / 'output.log').read_text()

    stime = time.time()
    while 'ssh listening' not in server_log():
        assert proc.poll() is None, server_log()
        assert time.time() - stime < 60, server_log()
        time.sleep(0.2)

    info = {'proc': proc, 'root': root, 'log': server_log}
    info.update(ports)
    yield info

    if proc.poll() is None:
        proc.kill()
        proc.wait()
    output.close()


def signup(port, handle, password):
    screen = Screen(port)
    screen.expect(r'Login:')
    screen.send(u'new\r')
    for prompt, value in ((r'Username', handle), (r'Origin', u'Earth'),
                          (r'E-mail', u''), (r'Password', password),
                          (r'Again', password)):
        screen.expect(prompt)
        screen.send(value + u'\r')
    screen.expect(r'Create account')
    screen.send(u'y\r')
    screen.expect(r'quick login', timeout=30)
    screen.send(u'y\r')
    screen.expect(r'logoff system', timeout=30)
    return screen


def test_telnet_signup_and_menus(server):
    screen = signup(server['telnet'], u'jojo', u'pass1234')
    # first user is sysop
    assert u'sysop' in screen.text
    # visit a few scripts and return to the main menu.
    for command, pattern, keys in (
            (u'lc', r'jojo', u'\r'),
            (u'who', r'keys', u'q'),
            (u'si', r'x/84', u' '),
    ):
        screen.raw = u''
        screen.send(command + u'\r')
        screen.expect(pattern)
        screen.read(0.5)
        screen.send(keys)
        screen.expect(r'logoff system')
        assert u'Traceback' not in screen.text, screen.text
    # logoff
    screen.send(u'g\r')
    screen.expect(r'SOMEthiNG')
    screen.send(u'g')
    screen.expect(r'mundane world')
    screen.read(3)
    assert screen.eof
    assert u'Traceback' not in server['log']()


def test_telnet_login_failure(server):
    screen = Screen(server['telnet'])
    screen.expect(r'Login:')
    screen.send(u'nobody\r')
    screen.expect(r'Password')
    screen.send(u'wrong\r')
    screen.expect(r'Login failed', timeout=30)
    screen.send(u'bye\r')
    screen.read(3)
    assert screen.eof
    screen.close()


def test_ssh_login(server):
    import paramiko
    signup(server['telnet'], u'sshuser', u'sshpass1').close()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    with pytest.raises(paramiko.AuthenticationException):
        client.connect('127.0.0.1', port=server['ssh'], username='sshuser',
                       password='wrong', look_for_keys=False,
                       allow_agent=False)
    client.connect('127.0.0.1', port=server['ssh'], username='SSHUSER',
                   password='sshpass1', look_for_keys=False,
                   allow_agent=False)
    channel = client.invoke_shell(term='xterm-256color', width=80, height=25)
    output = u''
    stime = time.time()
    while u'quick login' not in RE_SEQUENCE.sub(u'', output):
        assert time.time() - stime < 30, output
        if channel.recv_ready():
            output += channel.recv(65536).decode('utf8', 'replace')
        time.sleep(0.05)
    client.close()


def test_sigterm_shutdown(server):
    """ Must be last: SIGTERM ends the server gracefully. """
    screen = Screen(server['telnet'])
    screen.expect(r'Login:')
    server['proc'].send_signal(signal.SIGTERM)
    assert server['proc'].wait(timeout=20) == 0
    screen.read(2)
    assert screen.eof
    assert u'server shutdown' in server['log']()
