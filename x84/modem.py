"""
Modem server for x/84.

Dial-in modem callers reach x/84 through a softmodem, such as datamodem_,
which answers the call and runs ``x84-modem-bridge`` (see
:mod:`x84.modem_bridge`) for it.  The bridge connects to this server, on a
local port, and sends one line describing the call -- its connect rate,
modulation, and caller ID -- followed by the caller's data, untouched, in
both directions.  There is no telnet or rlogin protocol in the stream: a
modem caller's terminal sees exactly what the bbs sends.

To enable, add to default.ini::

    [modem]
    enabled = yes

The following options are available, but not required:

- ``addr``, ``port``: where to listen, ``127.0.0.1`` port ``6510`` by default.
  Only the bridge should connect here: do not listen on a public address.
- ``secret``: when set, the bridge must send the same value (option
  ``--secret`` or environment value ``X84_MODEM_SECRET``).
- ``term``: terminal type of modem callers, ``ansi`` by default.
- ``encoding``: character encoding of modem callers, ``cp437`` by default.
- ``art_min_rate``: artwork is not displayed to callers connected at a
  lower bit rate than this, ``2400`` by default; 0 always displays it.

.. _datamodem: https://github.com/billsimon/datamodem
"""
# std imports
import hmac
import json
import re
import select
import socket
import time

# local
from x84.client import BaseClient, BaseConnect
from x84.server import BaseServer
from x84.terminal import spawn_client_session

#: maximum length of the header line sent by the bridge.
HEADER_MAXLEN = 4096

#: header fields and the session environment variables they become.
HEADER_FIELDS = {
    'rate': 'MODEM_RATE',
    'modulation': 'MODEM_MODULATION',
    'protocol': 'MODEM_PROTOCOL',
    'connect': 'CONNECT',
    'caller_id': 'CALLER_ID',
    'caller_name': 'CALLER_NAME',
    'called_id': 'CALLED_ID',
}

#: maximum length of any header value.
VALUE_MAXLEN = 128

#: characters permitted in a caller ID used in a session-id.
RE_SID_UNSAFE = re.compile(r'[^0-9A-Za-z+#*._-]')


def clean_value(value):
    """
    Return header ``value`` as a printable str, at most VALUE_MAXLEN long.

    Values derive from the caller's own signalling (caller ID, name), and
    are destined for logs and the screens of other users.
    """
    if value is None:
        return u''
    value = u'{0}'.format(value)
    return u''.join(char for char in value
                    if char.isprintable())[:VALUE_MAXLEN]


def parse_header(line, secret=u''):
    """
    Parse header ``line`` sent by the bridge, returning a dict of call info.

    The header is a JSON object on one line, such as::

        {"rate": 14400, "caller_id": "+15551234", "protocol": "V.42/V.42bis"}

    Unknown fields are ignored; all values are cleaned by
    :func:`clean_value`.

    :param bytes line: header line, without its line terminator.
    :param str secret: when non-empty, the header's ``secret`` must match.
    :rtype: dict
    :raises ValueError: the header is malformed, or its secret is wrong.
    """
    try:
        header = json.loads(line.decode('utf8'))
    except (UnicodeDecodeError, ValueError) as err:
        raise ValueError('malformed modem header: {0}'.format(err))
    if not isinstance(header, dict):
        raise ValueError('malformed modem header: not an object')
    if secret and not hmac.compare_digest(
            u'{0}'.format(header.get('secret', u'')).encode('utf8'),
            secret.encode('utf8')):
        raise ValueError('modem header secret does not match')
    call = {key: clean_value(header.get(key)) for key in HEADER_FIELDS}
    try:
        call['rate'] = u'{0}'.format(int(call['rate'] or 0))
    except ValueError:
        call['rate'] = u'0'
    return call


class ModemClient(BaseClient):

    """ A modem caller, by way of the bridge. """

    kind = 'modem'

    #: Stop reading the session's output once this much is waiting to be
    #: sent: at modem speeds, anything more is minutes that the caller's
    #: keystrokes ("cancel", "next") must wait behind.
    SEND_HIGH_WATER = 2048

    #: socket buffer size requested of the operating system, for the same
    #: reason: the kernel's default would queue several minutes at 2400 bps.
    SOCKET_BUFFER = 4096

    def __init__(self, sock, address_pair, on_naws=None):
        super(ModemClient, self).__init__(sock, address_pair, on_naws)
        self.caller_id = u''
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                            self.SOCKET_BUFFER)
        except OSError:
            pass

    @property
    def addrport(self):
        """ Caller ID and local port of connection, as string. """
        caller = RE_SID_UNSAFE.sub(u'', self.caller_id) or u'unknown'
        return '%s:%d' % (caller, self.address_pair[1])

    def recv_ready(self):
        """ Whether data is awaiting on the socket. """
        return (self.is_active() and bool(
            select.select([self.sock.fileno()], [], [], 0)[0]))


class ConnectModem(BaseConnect):

    """ Read the bridge's header, then spawn a session for the caller. """

    #: maximum time to receive the header.
    TIME_NEGOTIATE = 5.0

    #: poll interval while receiving the header.
    TIME_POLL = 0.05

    def run(self):
        """ Receive header and spawn session. """
        from x84.bbs import get_ini
        try:
            self._set_socket_opts()
            call = parse_header(self.get_header(),
                                secret=get_ini('modem', 'secret') or u'')
            self.apply_environment(call)
            self.log.info('{client.addrport}: modem call, CONNECT {connect} '
                          'caller {caller}'.format(
                              client=self.client,
                              connect=call['connect'] or call['rate'],
                              caller=call['caller_id'] or 'unknown'))
            if self.client.is_active():
                return spawn_client_session(client=self.client)
        except ValueError as err:
            self.log.warning('{client.addrport}: {err}'
                             .format(client=self.client, err=err))
        except OSError as err:
            self.log.debug('{client.addrport}: connection closed: {err}'
                           .format(client=self.client, err=err))
        except Exception as err:
            self.log.exception('{client.addrport}: connection closed: {err}'
                               .format(client=self.client, err=err))
        finally:
            self.stopped = True
        self.client.deactivate()

    def get_header(self):
        """
        Receive the header line, returning it without its terminator.

        Anything received after it is the caller's, and is kept for the
        session.  The socket itself is read by the engine's main loop.
        :raises ValueError: no complete header within TIME_NEGOTIATE, or
                            it is longer than HEADER_MAXLEN.
        """
        data = bytearray()
        st_time = time.time()
        while True:
            if self.client.input_ready():
                data.extend(self.client.get_input())
            if b'\n' in data:
                header, _, rest = bytes(data).partition(b'\n')
                if rest:
                    self.client.unget_input(rest)
                return header.rstrip(b'\r')
            if len(data) > HEADER_MAXLEN:
                raise ValueError('modem header too long')
            if not self.client.is_active():
                raise OSError('closed before modem header')
            if time.time() - st_time >= self.TIME_NEGOTIATE:
                raise ValueError('no modem header within {0}s'
                                 .format(self.TIME_NEGOTIATE))
            time.sleep(self.TIME_POLL)

    def apply_environment(self, call):
        """ Set session environment by ``call`` info, and configuration. """
        from x84.bbs import get_ini
        env = self.client.env
        env['TERM'] = get_ini('modem', 'term') or 'ansi'
        env['encoding'] = get_ini('modem', 'encoding') or 'cp437'
        env['LINES'] = '24'
        env['COLUMNS'] = '80'
        for key, name in HEADER_FIELDS.items():
            env[name] = call[key]
        self.client.caller_id = call['caller_id']


class ModemServer(BaseServer):

    """ Server for modem calls, connected by ``x84-modem-bridge``. """

    client_factory = ModemClient
    connect_factory = ConnectModem
    config_section = 'modem'
    default_port = 6510

    #: all modem calls arrive from the bridge, on the local host: they are
    #: not subject to the connection limits of fail2ban.
    fail2ban = False

    def __init__(self, config):
        """ Class initializer. """
        if not config.has_option('modem', 'addr'):
            config.set('modem', 'addr', '127.0.0.1')
        BaseServer.__init__(self, config)
        if (self.address not in ('127.0.0.1', '::1', 'localhost') and
                not (config.has_option('modem', 'secret') and
                     config.get('modem', 'secret'))):
            self.log.warning('modem server listens on {0} without a secret: '
                             'anyone who can reach it may pose as a modem '
                             'caller, with any caller ID.'
                             .format(self.address))
