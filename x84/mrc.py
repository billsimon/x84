"""
Multi Relay Chat (MRC) relay for x/84.

MRC is a chat between BBSes: each BBS keeps one connection to the MRC
server, which carries the chat for all of its users.  When section
``[mrc]`` is enabled, the engine runs a :class:`Relay` for that
connection, and its :class:`Hub` passes packets between the relay and
the sessions running script ``mrcchat``, by events ``mrc``:

- a session sends ``('join', handle)`` on entering chat, ``('send',
  line)`` for each packet, and ``('leave',)`` on leaving it.
- a session receives ``('packet', line)`` for each packet from the
  server, and ``('status', state)`` for the relay's state, ``connected``,
  ``disconnected``, or ``disabled``.

A packet is a line of seven fields, each ended by ``~``::

    FROMUSER~FROMSITE~FROMROOM~TOUSER~MSGEXT~TOROOM~MESSAGE~

Options in section ``[mrc]``:

- ``enabled``: whether to connect, ``no``.
- ``server``, ``port``: the server, ``mrc.bottomlessabyss.net`` port
  ``5001``; its port without ssl is ``5000``.
- ``ssl``: whether to connect by TLS, ``yes``; ``ssl_verify``, whether to
  verify the server's certificate, ``yes``.
- ``bbsname``: the bbs's name, as listed by ``/bbses``, which allows pipe
  codes; the ``bbsname`` in section ``[system]`` by default.
- ``info_web``, ``info_telnet``, ``info_ssh``, ``info_sysop``,
  ``info_desc``: the bbs's information, as displayed by ``/info``.
"""
# std imports
import collections
import logging
import platform
import queue
import re
import socket
import ssl
import threading
import time
import unicodedata

#: version of the MRC protocol, following the Mystic client.
VERSION = '1.3.7'

#: platform of the bbs, told to the server.
PLATFORM = 'X84'

#: version of the client, answered to ``VERSION``.
CLIENT_VERSION = 'x/84 Multi Relay Chat v{0}'.format(VERSION)

#: fields of a packet.
Packet = collections.namedtuple('Packet', [
    'from_user', 'from_site', 'from_room', 'to_user', 'msg_ext',
    'to_room', 'body'])

#: names in a packet that are not users.
RESERVED = ('SERVER', 'CLIENT', 'NOTME')

#: seconds between attempts to connect, by failures in a row.
RETRY_INTERVALS = (1, 5, 10, 30, 60, 90, 120, 180, 240, 300)

#: seconds between requests for the server's statistics.
STATS_INTERVAL = 20

#: seconds to wait for the server to connect, or to accept what is sent.
CONNECT_TIMEOUT = 30

#: seconds to wait for what the server sends, between sending.
POLL_TIMEOUT = 0.1

#: longest name of a user or room that the protocol allows.
MAX_NAME = 30

#: packet keywords for the bbs's information, and their options in
#: section [mrc].
INFO_FIELDS = (('INFOWEB', 'info_web'), ('INFOTEL', 'info_telnet'),
               ('INFOSSH', 'info_ssh'), ('INFOSYS', 'info_sysop'),
               ('INFODSC', 'info_desc'))

RE_PIPE = re.compile(r'\|[0-9]{2}')

#: the engine's hub, set by :func:`start`.
HUB = None


def strip_pipes(text):
    """ Return ``text`` without color pipe codes, such as ``|07``. """
    return RE_PIPE.sub(u'', text)


def ascii_text(text):
    """
    Return ``text`` as printable ascii, which MRC requires: a character
    becomes its letter without an accent, or else ``?``; ``~``, which ends
    a field, becomes a space, and any control character is removed.
    """
    result = []
    for ucs in unicodedata.normalize('NFC', text):
        if unicodedata.combining(ucs):
            continue
        base = unicodedata.normalize('NFKD', ucs)[:1]
        if ucs == u'~':
            result.append(u' ')
        elif u' ' <= base < u'~':
            result.append(base)
        elif not unicodedata.category(ucs).startswith('C'):
            result.append(u'?')
    return u''.join(result)


def name_tag(name):
    """
    Return ``name`` as a user, bbs or room name for a packet: without
    pipe codes, spaces changed to ``_``, at most :data:`MAX_NAME` long.
    """
    return ascii_text(strip_pipes(name)).strip().replace(u' ', u'_')[
        :MAX_NAME]


def parse_packet(line):
    """ Parse ``line`` as :class:`Packet`, or None if too few fields. """
    fields = line.rstrip(u'\r\n').split(u'~')
    if len(fields) < 7:
        return None
    return Packet(*fields[:7])


def format_packet(packet):
    """ Return :class:`Packet` ``packet`` as a line. """
    return u'~'.join(ascii_text(value) for value in packet) + u'~'


def decode_line(raw):
    """ Decode bytes ``raw`` received: utf-8, else cp437. """
    try:
        return raw.decode('utf8')
    except UnicodeDecodeError:
        return raw.decode('cp437')


def bbs_name():
    """ Return the bbs's name as configured, with its pipe codes. """
    from x84.bbs import get_ini
    name = get_ini('mrc', 'bbsname') or get_ini('system', 'bbsname')
    return ascii_text(name or u'x/84').strip()


def site_tag():
    """ Return the bbs's name as the site field of a packet. """
    return name_tag(bbs_name())


def server_addresses(address):
    """ Return the addresses for ``(host, port)``, IPv4 first. """
    infos = socket.getaddrinfo(address[0], address[1],
                               type=socket.SOCK_STREAM)
    return sorted(infos, key=lambda info: info[0] != socket.AF_INET)


def connect_socket(address, timeout):
    """ Return socket connected to ``(host, port)``, at the first address
    that connects within ``timeout``. """
    error = OSError('no address for {0}'.format(address[0]))
    for family, kind, proto, _, sockaddr in server_addresses(address):
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
        except OSError as err:
            sock.close()
            error = err
            continue
        return sock
    raise error


class TooOld(Exception):

    """ The server refuses this version of the protocol. """


class Relay(threading.Thread):

    """
    The bbs's connection to the MRC server, reconnecting when it is lost.

    Packets from sessions, given to :meth:`send`, are sent to the server,
    except requests for latency and statistics, which this relay answers
    itself; it also answers ``VERSION``.  What the server sends is put in
    :attr:`received`, as ``(sid, payload)``, with sid None for every
    session in chat.
    """

    def __init__(self, server, port, use_ssl=True, ssl_verify=True,
                 bbsname=u'x/84', info=None):
        super().__init__(name='mrc-relay', daemon=True)
        self.log = logging.getLogger(__name__)
        self.address = (server, port)
        self.use_ssl = use_ssl
        self.ssl_verify = ssl_verify
        self.bbsname = bbsname
        self.site = name_tag(bbsname)
        #: the bbs's information, as ``(packet keyword, text)`` pairs.
        self.info = info or ()
        self.received = queue.Queue()
        self.outgoing = queue.Queue()
        self.state = 'disconnected'
        #: the server's statistics: bbses, rooms, users, and activity.
        self.stats = u''
        #: milliseconds for a packet sent to be received again.
        self.latency = 0
        self.stopped = threading.Event()
        self.sock = None
        self._sent = {}
        self._buffer = b''

    # used by the hub

    def send(self, sid, line):
        """ Send packet ``line`` from session ``sid``. """
        self.outgoing.put((sid, line))

    def stop(self):
        """ Disconnect, and stop. """
        self.stopped.set()

    def set_state(self, state):
        """ Change the state, telling every session in chat. """
        self.state = state
        self.received.put((None, ('status', state)))

    # connection

    def run(self):
        failures = 0
        while not self.stopped.is_set():
            try:
                self.sock = self.connect()
            except OSError as err:
                self.log.warning('mrc: connecting to {0}:{1}: {2}'.format(
                    self.address[0], self.address[1], err))
            else:
                started = time.time()
                try:
                    self.serve()
                except TooOld:
                    self.set_state('disabled')
                    return
                if time.time() - started > max(RETRY_INTERVALS):
                    failures = 0
            if self.stopped.is_set():
                break
            delay = RETRY_INTERVALS[min(failures, len(RETRY_INTERVALS) - 1)]
            failures += 1
            self.log.info('mrc: reconnecting in {0} seconds'.format(delay))
            self.stopped.wait(delay)

    def connect(self):
        """ Return socket connected to the server. """
        self.log.info('mrc: connecting to {0}:{1}{2}'.format(
            self.address[0], self.address[1],
            u' by ssl' if self.use_ssl else u''))
        sock = connect_socket(self.address, CONNECT_TIMEOUT)
        if self.use_ssl:
            context = ssl.create_default_context()
            if not self.ssl_verify:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            try:
                sock = context.wrap_socket(
                    sock, server_hostname=self.address[0])
            except OSError:
                sock.close()
                raise
        return sock

    def handshake(self):
        """ Return the line the bbs sends to begin the connection. """
        system = u'{0}_{1}'.format(platform.system(), platform.machine())
        return u'{0}~{1}/{2}/{3}'.format(
            self.bbsname.replace(u'~', u''), PLATFORM,
            system.replace(u' ', u'_'), VERSION)

    def serve(self):
        """ Relay packets, until disconnected or stopped. """
        self._buffer = b''
        self._sent.clear()
        # packets that sessions sent before connecting are stale.
        while not self.outgoing.empty():
            self.outgoing.get_nowait()
        try:
            self.write(self.handshake())
            for keyword, text in self.info:
                self.write_bbs(u'{0}:{1}'.format(keyword, text),
                               to_site=u'ALL')
            self.send_alive()
            self.write_bbs(u'CAPABILITIES:MCI CTCP{0}'.format(
                u' SSL' if self.use_ssl else u''))
            self.log.info('mrc: connected to {0}:{1}'.format(*self.address))
            self.set_state('connected')
            next_stats = time.time() + 2
            while not self.stopped.is_set():
                self.sock.settimeout(POLL_TIMEOUT)
                try:
                    data = self.sock.recv(16384)
                except socket.timeout:
                    pass
                else:
                    if not data:
                        raise ConnectionError('closed by the server')
                    self.receive(data)
                self.forward()
                if time.time() >= next_stats:
                    self.write_bbs(u'STATS')
                    next_stats = time.time() + STATS_INTERVAL
            self.write_bbs(u'SHUTDOWN')
        except OSError as err:
            self.log.warning('mrc: disconnected: {0}'.format(err))
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
            if self.state != 'disconnected':
                self.set_state('disconnected')

    def write(self, line):
        """ Send ``line`` to the server. """
        self.sock.settimeout(CONNECT_TIMEOUT)
        self.sock.sendall(line.encode('ascii', 'replace') + b'\n')

    def write_bbs(self, body, to_site=u''):
        """ Send a packet from the bbs to the server, with ``body``. """
        self.write(format_packet(Packet(
            u'CLIENT', self.site, u'', u'SERVER', to_site, u'', body)))

    def send_alive(self):
        """ Tell the server the bbs is connected. """
        self.write_bbs(u'IMALIVE:{0}'.format(self.site))

    # received

    def receive(self, data):
        """ Handle ``data`` received, line by line. """
        lines = (self._buffer + data).replace(b'\r', b'\n').split(b'\n')
        self._buffer = lines.pop()
        if len(self._buffer) > 65536:
            self._buffer = b''
        for raw in lines:
            if raw.strip():
                self.handle_line(decode_line(raw))

    def handle_line(self, line):
        """ Handle ``line`` from the server. """
        key = line.strip()
        if key in self._sent:
            self.latency = int((time.time() - self._sent[key]) * 1000)
            self._sent.clear()
        packet = parse_packet(line)
        if packet is None:
            self.log.debug('mrc: not a packet: {0!r}'.format(line))
            return
        if packet.from_user == u'SERVER':
            body = packet.body
            if body.lower() == u'ping':
                self.send_alive()
                return
            if body.startswith(u'NEWUPDATE:'):
                self.log.info('mrc: version {0} of the protocol is '
                              'available'.format(body.partition(u':')[2]))
                return
            if body.startswith(u'OLDVERSION:'):
                self.log.error('mrc: the server refuses version {0} of '
                               'the protocol; it answered {1}'.format(
                                   VERSION, body.partition(u':')[2]))
                raise TooOld(body)
            if body.startswith(u'STATS:'):
                self.stats = body.partition(u':')[2]
        self.received.put((None, ('packet', line)))

    # sent

    def forward(self):
        """ Send packets from sessions, or answer them. """
        while True:
            try:
                sid, line = self.outgoing.get_nowait()
            except queue.Empty:
                return
            packet = parse_packet(line)
            if packet is None:
                continue
            if packet.to_user == u'CLIENT' and packet.body in (
                    u'LATENCY', u'STATS'):
                value = (self.latency if packet.body == u'LATENCY'
                         else self.stats)
                self.reply(sid, Packet(
                    u'SERVER', u'', u'', u'CLIENT', self.site, u'',
                    u'{0}:{1}'.format(packet.body, value)))
                continue
            if packet.body == u'VERSION':
                self.reply(sid, Packet(
                    u'CLIENT', u'', u'', packet.from_user, packet.from_site,
                    u'', u'|07- {0}'.format(CLIENT_VERSION)))
            line = format_packet(packet)
            if len(self._sent) > 100:
                self._sent.clear()
            self._sent[line] = time.time()
            self.write(line)

    def reply(self, sid, packet):
        """ Answer session ``sid`` by ``packet``. """
        self.received.put((sid, ('packet', format_packet(packet))))


class Hub(object):

    """
    The sessions in chat and the relay they share, in the engine's thread.

    A session that ends without leaving chat is logged off the server.
    """

    def __init__(self, relay=None):
        self.relay = relay
        #: the last packet of each session in chat, by its sid.
        self.sessions = {}
        self.pending = collections.deque()

    def status(self):
        """ The relay's state. """
        return self.relay.state if self.relay is not None else 'disabled'

    def session_event(self, sid, data):
        """ Handle event ``mrc`` from session ``sid``. """
        verb = data[0] if data else None
        if verb == 'join':
            self.sessions[sid] = None
            self.pending.append((sid, ('status', self.status())))
        elif verb == 'leave':
            self.sessions.pop(sid, None)
        elif verb == 'send' and sid in self.sessions:
            packet = parse_packet(data[1])
            if packet is not None and self.relay is not None:
                self.sessions[sid] = packet
                self.relay.send(sid, data[1])

    def deliver(self, terminals):
        """ Send what is received to the sessions in ``terminals``. """
        ttys = dict(terminals)
        for sid in [sid for sid in self.sessions if sid not in ttys]:
            self.log_off(self.sessions.pop(sid))
        messages = []
        while self.pending:
            messages.append(self.pending.popleft())
        if self.relay is not None:
            while True:
                try:
                    messages.append(self.relay.received.get_nowait())
                except queue.Empty:
                    break
        for target, payload in messages:
            for sid in ([target] if target is not None else
                        list(self.sessions)):
                if sid in self.sessions and sid in ttys:
                    try:
                        ttys[sid].master_write.send(('mrc', payload))
                    except OSError:
                        # the session has just disconnected.
                        pass

    def log_off(self, packet):
        """ Log off the user of an ended session, by its last ``packet``. """
        if packet is None or self.relay is None:
            return
        self.relay.send(None, format_packet(Packet(
            packet.from_user, packet.from_site, packet.from_room,
            u'NOTME', u'', u'', u'|07- |12{0} |04has left chat.'.format(
                packet.from_user))))
        self.relay.send(None, format_packet(Packet(
            packet.from_user, packet.from_site, packet.from_room,
            u'SERVER', packet.from_site, packet.from_room, u'LOGOFF')))


def start():
    """ Start the relay set in section ``[mrc]``, when enabled. """
    # pylint: disable=W0603
    #         Using the global statement
    global HUB
    from x84.bbs import get_ini
    relay = None
    if get_ini('mrc', 'enabled', getter='getboolean'):
        use_ssl = get_ini('mrc', 'ssl', getter='getboolean')
        port = get_ini('mrc', 'port', getter='getint') or (
            5001 if use_ssl else 5000)
        ssl_verify = (get_ini('mrc', 'ssl_verify', getter='getboolean')
                      if get_ini('mrc', 'ssl_verify') else True)
        info = tuple((keyword, ascii_text(get_ini('mrc', key) or u''))
                     for keyword, key in INFO_FIELDS
                     if get_ini('mrc', key))
        relay = Relay(get_ini('mrc', 'server') or 'mrc.bottomlessabyss.net',
                      port, use_ssl=use_ssl, ssl_verify=ssl_verify,
                      bbsname=bbs_name(), info=info)
        relay.start()
    HUB = Hub(relay)
    return HUB


def stop():
    """ Stop the relay, disconnecting from the server. """
    if HUB is not None and HUB.relay is not None:
        HUB.relay.stop()
        HUB.relay.join(CONNECT_TIMEOUT)
