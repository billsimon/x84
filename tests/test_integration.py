"""
Integration tests: a real x/84 server, driven by telnet and ssh clients.

Each test module run starts one server, in a subprocess, on free ports with
a temporary configuration and database.
"""
# std imports
import os
import re
import shutil
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
def server(tmp_path_factory, request, tls_cert):
    """ Start x84 server on free ports, return dict of ports and paths. """
    import socket
    import x84.bbs.ini

    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    root = tmp_path_factory.mktemp('x84')
    ports = {'telnet': free_port(), 'ssh': free_port(), 'modem': free_port(),
             'tn3270': free_port(), 'tn3270_tls': free_port()}
    cfg = x84.bbs.ini.init_bbs_ini()
    cfg.set('system', 'datapath', str(root / 'data'))
    cfg.set('system', 'password_digest', 'internal')
    cfg.set('telnet', 'port', str(ports['telnet']))
    cfg.set('ssh', 'enabled', 'yes')
    cfg.set('ssh', 'port', str(ports['ssh']))
    cfg.set('ssh', 'hostkey', str(root / 'ssh_host_rsa_key'))
    cfg.set('ssh', 'hostkeybits', '2048')
    cfg.set('modem', 'enabled', 'yes')
    cfg.set('modem', 'port', str(ports['modem']))
    cfg.set('modem', 'secret', 'modem-secret')
    cfg.set('tn3270', 'enabled', 'yes')
    cfg.set('tn3270', 'port', str(ports['tn3270']))
    cfg.set('tn3270', 'tls_port', str(ports['tn3270_tls']))
    cfg.set('tn3270', 'cert', tls_cert[0])
    cfg.set('tn3270', 'key', tls_cert[1])
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
    while 'tn3270 tls listening' not in server_log():
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


def bridge_call(server, environ, secret='modem-secret'):
    """ Start x84-modem-bridge as datamodem's --exec would. """
    import os
    env = dict(os.environ, X84_MODEM_SECRET=secret, **environ)
    return subprocess.Popen(
        [sys.executable, '-m', 'x84.modem_bridge', '--port',
         str(server['modem'])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env)


def read_until(proc, pattern, timeout=30):
    """ Read bridge stdout until ``pattern``, returning screen text. """
    import select
    raw = b''
    stime = time.time()
    while time.time() - stime < timeout:
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if ready:
            data = os.read(proc.stdout.fileno(), 4096)
            if not data:
                break
            raw += data
            text = RE_SEQUENCE.sub(u'', raw.decode('cp437', 'replace'))
            if re.search(pattern, text):
                return text
    raise AssertionError('{0!r} not received:\n{1}'.format(
        pattern, RE_SEQUENCE.sub(u'', raw.decode('cp437', 'replace'))))


def test_modem_call(server):
    """ A modem caller, by way of the bridge, reaches the login prompt. """
    proc = bridge_call(server, {'CONNECT': '1200', 'MODEM_RATE': '1200',
                                'CALLER_ID': '+15551234', 'CALLER_NAME': 'Ann',
                                'MODEM_MODULATION': 'v22'})
    text = read_until(proc, r'Login:')
    assert u'CONNECT 1200' in text
    assert u'matrix.ans not shown at 1200 bps' in text
    assert u'[modem-+15551234:' in server['log']()

    # the bbs ends the session ('bye'): the bridge exits, and the modem hangs
    # up when it does.
    proc.stdin.write(b'bye\r')
    proc.stdin.flush()
    assert proc.wait(timeout=30) == 0
    assert b'the bbs closed the connection' in proc.stderr.read()


def test_modem_caller_hangs_up(server):
    """ The caller hangs up: the bridge's stdin ends, and so does the session. """
    proc = bridge_call(server, {'CONNECT': '14400 V.42/V.42bis',
                                'CALLER_ID': '5550199'})
    text = read_until(proc, r'Login:')
    # art is shown at 14400.
    assert u'not shown' not in text
    proc.stdin.close()
    assert proc.wait(timeout=30) == 0
    stime = time.time()
    while u'[modem-5550199:' not in server['log']() or not re.search(
            r'\[modem-5550199:\d+\] goodbye', server['log']()):
        assert time.time() - stime < 20, server['log']()[-2000:]
        time.sleep(0.2)


def test_modem_bridge_wrong_secret(server):
    proc = bridge_call(server, {'CONNECT': '2400'}, secret='wrong')
    proc.stdin.close()
    proc.wait(timeout=30)
    assert proc.stdout.read() == b''
    stime = time.time()
    while u'modem header secret does not match' not in server['log']():
        assert time.time() - stime < 20, server['log']()[-2000:]
        time.sleep(0.1)


def test_post_and_quote_reply(server):
    """ Post a message with the editor, then reply quoting it. """
    screen = signup(server['telnet'], u'poster', u'pass1234')
    screen.send(u'msg\r')
    # tags subscribed to are chosen at first: all.
    screen.expect(r'subscription tags')
    screen.send(u'\r')
    screen.expect(r'ost public')

    # post: recipient (all), subject, body by editor, and tags (none).
    screen.send(u'p\r')
    screen.expect(r'To:')
    screen.send(u'\r')
    screen.expect(r'Subject:')
    screen.send(u'hello\r')
    screen.expect(r'\^Z Save')
    screen.send(u'first line\rsecond line')
    screen.send(u'\x1a')
    screen.expect(r'Tags of your message')
    screen.send(u'\r')
    screen.expect(r'Posted')

    # the board lists it: reply quoting its second line.
    screen.raw = u''
    screen.send(u'r')
    screen.expect(r'\^Q Quote')
    assert u'Re: hello' in screen.text
    screen.send(u'\x11')
    screen.expect(r'Enter inserts')
    screen.send(u'\x1b[B')
    screen.send(u'\r')
    screen.send(u'my reply')
    screen.send(u'\x1a')
    screen.expect(r'Posted')

    # the reply is selected: read it.
    screen.raw = u''
    screen.send(u'\r')
    screen.expect(r'my reply')
    assert u'poster wrote:' in screen.text
    assert u'> second line' in screen.text
    assert u'> first line' not in screen.text
    screen.send(u'q')
    screen.send(u'q')
    screen.expect(r'ost public')
    assert u'Traceback' not in server['log']()
    screen.close()


def login(port, handle, password):
    screen = Screen(port)
    screen.expect(r'Login:')
    screen.send(handle + u'\r')
    screen.expect(r'Password')
    screen.send(password + u'\r')
    screen.expect(r'quick login', timeout=30)
    screen.send(u'y\r')
    screen.expect(r'logoff system', timeout=30)
    return screen


def test_private_mail(server):
    """ Mail is sent by one user, and read by another. """
    signup(server['telnet'], u'marty', u'pass1234').close()
    screen = signup(server['telnet'], u'doc', u'pass1234')
    screen.send(u'mail\r')
    screen.expect(r'No messages')
    screen.send(u'w')
    screen.expect(r'To:')
    screen.send(u'marty\r')
    screen.expect(r'Subject:')
    screen.send(u'flux\r')
    screen.expect(r'\^Z Save')
    screen.send(u'1.21 gigawatts')
    screen.send(u'\x1a')
    screen.expect(r'Sent to marty')
    screen.send(u'q')
    screen.expect(r'logoff system')
    screen.close()

    screen = login(server['telnet'], u'marty', u'pass1234')
    screen.raw = u''
    screen.send(u'mail\r')
    screen.expect(r'Inbox, 1 new')
    screen.expect(r'flux')
    screen.send(u'\r')
    screen.expect(r'1\.21 gigawatts')
    screen.send(u'q')
    screen.send(u'q')
    screen.expect(r'logoff system')
    assert u'Traceback' not in server['log']()
    screen.close()


class S3270(object):

    """ A 3270 terminal, by s3270 of the x3270 family. """

    def __init__(self, port, model='3279-2-E', tls_ca=None):
        """ Connect to ``port``, by TLS when given ``tls_ca``. """
        args = ['s3270', '-model', model, '-utf8']
        if tls_ca:
            args += ['-cafile', tls_ca]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.cmd('Connect({0}127.0.0.1:{1})'.format(
            u'L:' if tls_ca else u'', port))

    def cmd(self, command):
        """ Run s3270 ``command``, returning its lines of data. """
        self.proc.stdin.write(command + '\n')
        self.proc.stdin.flush()
        data = []
        while True:
            line = self.proc.stdout.readline()
            assert line, 'end of s3270 output, after {0}'.format(command)
            line = line.rstrip('\n')
            if line == 'ok':
                return data
            if line == 'error':
                raise AssertionError('{0}: {1}'.format(command, data))
            if line.startswith('data: '):
                data.append(line[len('data: '):])

    def type(self, text):
        """ Type ``text``, once the keyboard is unlocked. """
        self.cmd('Wait(10,Unlock)')
        self.cmd('String("{0}")'.format(text))

    @property
    def text(self):
        return u'\n'.join(self.cmd('Ascii()'))

    def find(self, text):
        """ Return (row, column) of ``text`` on screen. """
        for row, line in enumerate(self.cmd('Ascii()')):
            if text in line:
                return row, line.index(text)
        raise AssertionError('{0!r} not on screen:\n{1}'.format(
            text, self.text))

    def enter_at(self, row, col):
        """ Move the cursor to (row, col), and press ENTER. """
        self.cmd('Wait(10,Unlock)')
        self.cmd('MoveCursor({0},{1})'.format(row, col))
        self.cmd('Enter')

    def expect(self, pattern, timeout=20):
        stime = time.time()
        while time.time() - stime < timeout:
            if re.search(pattern, self.text):
                return
            time.sleep(0.2)
        raise AssertionError('{0!r} not on screen:\n{1}'.format(
            pattern, self.text))

    def close(self):
        self.proc.stdin.write('Quit\n')
        self.proc.stdin.flush()
        self.proc.wait(timeout=10)


@pytest.mark.skipif(not shutil.which('s3270'),
                    reason='s3270, of the x3270 family, is not installed')
def test_tn3270_login(server):
    """ A 3270 terminal logs in, posts a message, and reads it. """
    signup(server['telnet'], u'ibm', u'pass1234').close()
    term = S3270(server['tn3270'])
    term.expect(r'PRESS ENTER TO LOG ON')
    term.cmd('Enter')
    term.expect(r'Login:')
    term.type(u'ibm')
    term.cmd('Enter')
    term.expect(r'Password')
    term.type(u'pass1234')
    # the password is not displayed, as it is typed.
    assert u'pass1234' not in term.text
    term.cmd('Enter')
    term.expect(r'quick login', timeout=30)
    term.type(u'y')
    term.cmd('Enter')
    term.expect(r'logoff system', timeout=30)

    # the message area is chosen by the cursor, on its menu item; and a
    # post, typing in the editor's window.
    term.enter_at(*term.find(u'[msg] message area'))
    term.expect(r'subscription tags')
    term.cmd('Enter')
    term.expect(r'ost public')
    term.type(u'p')
    term.cmd('Enter')
    term.expect(r'To:')
    term.cmd('Enter')
    term.expect(r'Subject:')
    term.type(u'from a 3270')
    term.cmd('Enter')
    term.expect(r'PF3 Menu')
    term.type(u'first line')
    term.cmd('Enter')
    term.type(u'second line')
    term.cmd('PF(3)')
    term.expect(r'Menu: S\)ave')
    term.type(u's')
    term.cmd('Enter')
    term.expect(r'Tags of your message')
    term.cmd('Enter')
    term.expect(r'Posted')

    # read it, by the cursor; Q returns to the list, without a return key
    # that would read it again.
    term.enter_at(2, 20)
    term.expect(r'second line')
    assert u'first line' in term.text
    term.type(u'q')
    term.cmd('Enter')
    term.expect(r'Q Quit')
    time.sleep(1)
    assert u'second line' not in term.text
    # quit, by the cursor on the key of the status bar.
    term.enter_at(*term.find(u'Q Quit'))
    term.expect(r'ost public')
    assert u'tn3270-127.0.0.1:' in server['log']()
    assert u'Traceback' not in server['log']()
    term.close()


@pytest.mark.skipif(not shutil.which('s3270'),
                    reason='s3270, of the x3270 family, is not installed')
def test_tn3270_tls(server, tls_cert):
    """ A 3270 terminal connects by TLS, verifying the certificate. """
    term = S3270(server['tn3270_tls'], tls_ca=tls_cert[0])
    term.expect(r'PRESS ENTER TO LOG ON')
    assert term.cmd('Query(Tls)') == [u'secure host-verified']
    term.cmd('Enter')
    term.expect(r'Login:')
    term.close()
    assert u'Traceback' not in server['log']()


def test_sigterm_shutdown(server):
    """ Must be last: SIGTERM ends the server gracefully. """
    screen = Screen(server['telnet'])
    screen.expect(r'Login:')
    server['proc'].send_signal(signal.SIGTERM)
    assert server['proc'].wait(timeout=20) == 0
    screen.read(2)
    assert screen.eof
    assert u'server shutdown' in server['log']()

