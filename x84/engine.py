#!/usr/bin/env python
""" Command-line launcher and event loop for x/84. """
# Place ALL metadata in setup.py, except where not suitable, place here.
# For any contributions, feel free to tag __author__ etc. at top of such file.
__author__ = "Johannes Lundberg (jojo), Jeff Quast (dingo)"
__url__ = u'https://github.com/jquast/x84/'
__copyright__ = "Copyright 2003"
__credits__ = [
    # use 'scene' names unless preferred or unavailable.
    "zipe",
    "jojo",
    "maze",
    "dingo",
    "spidy",
    "beardy",
    "haliphax",
    "megagumbo",
    "hellbeard",
    "Mercyful Fate",
]
__license__ = 'ISC'

# std
import logging
import pickle
import select
import signal
import socket
import sys

# local
import x84.encodings  # noqa  provides alternate encodings
from x84 import cmdline
from x84.db import DBHandler, LOCKS, thread_holder_alive, close_databases
from x84.terminal import get_terminals, kill_session, find_tty
from x84.fail2ban import get_fail2ban_function


def main():
    """
    x84 main entry point. The system begins and ends here.

    Command line arguments to engine.py:

    - ``--config=`` location of alternate configuration file
    - ``--logger=`` location of alternate logging.ini file
    """
    # load existing .ini files or create default ones.
    import x84.bbs.ini
    x84.bbs.ini.init(*cmdline.parse_args())

    from x84.bbs import get_ini
    from x84.bbs.ini import CFG

    # exit gracefully on SIGTERM (such as by systemd or docker), as for ^C.
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # retrieve list of managed servers
    servers = get_servers(CFG)

    # begin unmanaged servers
    if server_enabled(CFG, 'web'):
        # start https server for one or more web modules.
        from x84 import webserve
        webserve.main()

    if get_ini(section='msg', key='network_tags'):
        # start background timer to poll for new messages
        # of message networks we may be a member of.
        from x84 import msgpoll
        msgpoll.main()

    try:
        # begin main event loop
        _loop(servers)
    except KeyboardInterrupt:
        # exit on ^C or SIGTERM, killing any client sessions.
        log = logging.getLogger('x84.engine')
        log.info('server shutdown')
        shutdown(servers)
    return 0


def _sigterm_handler(signum, frame):
    """ Signal handler for SIGTERM, raises KeyboardInterrupt. """
    # pylint: disable=W0613
    #         Unused argument
    raise KeyboardInterrupt('SIGTERM')


def shutdown(servers):
    """ Kill all client sessions, close server sockets and databases. """
    for server in servers:
        for thread in server.threads[:]:
            thread.stopped = True
            server.threads.remove(thread)
        for key, client in list(server.clients.items()):
            kill_session(client, 'server shutdown')
            del server.clients[key]
        server.server_socket.close()
    close_databases()


def server_enabled(CFG, section):
    """
    Whether server of configuration ``section`` is enabled.

    A server is enabled when its section exists, and its ``enabled`` option
    is either missing or true.
    """
    return CFG.has_section(section) and (
        not CFG.has_option(section, 'enabled')
        or CFG.getboolean(section, 'enabled'))


def get_servers(CFG):
    """ Instantiate and return enabled servers by configuration ``CFG``. """
    servers = []

    if server_enabled(CFG, 'telnet'):
        # start telnet server instance
        from x84.telnet import TelnetServer
        servers.append(TelnetServer(config=CFG))

    if server_enabled(CFG, 'ssh'):
        # start ssh server instance
        #
        # may raise an ImportError for systems where pyOpenSSL and etc. could
        # not be installed (due to any issues with missing python-dev, libffi,
        # cc, etc.).  Allow it to raise naturally, the curious user should
        # either discover and resolve the root issue, or disable ssh if it
        # cannot be resolved.
        from x84.ssh import SshServer
        servers.append(SshServer(config=CFG))

    if server_enabled(CFG, 'rlogin'):
        # start rlogin server instance
        from x84.rlogin import RLoginServer
        servers.append(RLoginServer(config=CFG))

    return servers


def find_server(servers, fd):
    """ Find matching ``server.server_socket`` for given file descriptor. """
    for server in servers:
        if fd == server.server_socket.fileno():
            return server


def accept(log, server, check_ban):
    """
    Accept new connection from server, spawning an unmanaged thread.

    Connecting socket accepted is server.server_socket, instantiate a
    new instance of client_factory, with optional keyword arguments
    defined by server.client_factory_kwargs, registering it with
    dictionary server.clients, and spawning an unmanaged thread
    using connect_factory, with optional keyword arguments
    server.connect_factory_kwargs.
    """
    if None in (server.client_factory, server.connect_factory):
        raise NotImplementedError(
            "No accept for server class {server.__class__.__name__}"
            .format(server=server))

    client_factory_kwargs = server.client_factory_kwargs
    if callable(server.client_factory_kwargs):
        client_factory_kwargs = server.client_factory_kwargs(server)

    connect_factory_kwargs = server.connect_factory_kwargs
    if callable(server.connect_factory_kwargs):
        connect_factory_kwargs = server.connect_factory_kwargs(server)

    try:
        sock, address_pair = server.server_socket.accept()

        # busy signal
        if server.client_count() >= server.MAX_CONNECTIONS:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            log.error('{addr}: refused, maximum connections reached.'
                      .format(addr=address_pair[0]))
            return

        # connecting IP is banned
        if check_ban(address_pair[0]) is False:
            log.debug('{addr}: refused, banned.'.format(addr=address_pair[0]))
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            return

        # instantiate a client of this type
        client = server.client_factory(sock, address_pair,
                                       **client_factory_kwargs)

        # spawn on-connect negotiation thread.  When successful,
        # a new sub-process is spawned and registered as a session tty.
        server.clients[client.sock.fileno()] = client
        thread = server.connect_factory(client, **connect_factory_kwargs)
        log.info('{client.kind} connection from {client.addrport} '
                 '(*{thread.name}).'.format(client=client, thread=thread))
        server.threads.append(thread)
        thread.start()

    except OSError as err:
        log.error('accept error: {0}'.format(err))


def get_session_output_fds(servers):
    """ Return file descriptors of all ``tty.master_read`` pipes. """
    session_fds = []
    for server in servers:
        for client in server.clients.values():
            tty = find_tty(client)
            if tty is not None:
                try:
                    session_fds.append(tty.master_read.fileno())
                except OSError:
                    # pipe was closed
                    pass
    return session_fds


def client_recv(servers, ready_fds, log):
    """
    Test all clients for recv_ready().

    If any data is available, then ``client.socket_recv()`` is called,
    buffering the data for the session which is exhausted by
    :func:`session_send`.
    """
    from x84.bbs.exception import Disconnected
    for server in servers:
        for client in server.clients_ready(ready_fds):
            try:
                client.socket_recv()
            except Disconnected as err:
                log.debug('{client.addrport}: disconnect on recv: {err}'
                          .format(client=client, err=err))
                kill_session(client, 'disconnected: {err}'.format(err=err))


def client_send(terminals, log):
    """
    Test all clients for send_ready().

    If any data is available, then ``tty.client.send()`` is called.
    This is data sent from the session to the tcp client.
    """
    from x84.bbs.exception import Disconnected
    # nothing to send until tty is registered.
    for _, tty in terminals:
        if tty.client.send_ready():
            try:
                tty.client.send()
            except Disconnected as err:
                log.debug('{client.addrport}: disconnect on send: {err}'
                          .format(client=tty.client, err=err))
                kill_session(tty.client, 'disconnected: {err}'.format(err=err))


def session_send(terminals):
    """
    Test all tty clients for input_ready().

    Meaning, tcp data has been buffered to be received by the tty session,
    and send it to the tty input queue (tty.master_write).  Also, test all
    sessions for idle timeout, signaling exit to subprocess when reached.
    """
    for _, tty in terminals:
        if tty.client.input_ready():
            try:
                tty.master_write.send(('input', tty.client.get_input()))
            except OSError:
                # this may happen if a sub-process crashes, or more often,
                # because the subprocess has logged off, but the user kept
                # banging the keyboard before we have had the opportunity
                # to close their telnet socket.
                kill_session(tty.client, 'no tty for socket data')

        # poll about and kick off idle users
        elif tty.timeout and tty.client.idle() > tty.timeout:
            kill_session(tty.client, 'timeout')


def handle_lock(locks, tty, event, data, tap_events, log):
    """
    Handle locking event of ``(lock-key, (method, stale))``.

    :param x84.db.LockTable locks: bbs-wide lock table.
    :param x84.terminal.TerminalProcess tty: requesting session.
    :param str event: name of lock.
    :param tuple data: ``(method, stale)``, where method is ``'acquire'`` or
        ``'release'``, and stale is ``None`` or number of seconds after which
        a lock held by another session may be taken.
    """
    # pylint: disable=R0913
    #         Too many arguments (6/5)
    method, stale = data
    if method == 'acquire':
        active_sids = set(_sid for _sid, _ in get_terminals())

        def is_alive(holder):
            """ Whether lock holder is an active session or thread. """
            return holder in active_sids or thread_holder_alive(holder)

        acquired, holder, elapsed = locks.acquire(
            event, tty.sid, stale=stale, is_alive=is_alive)
        if acquired and holder not in (None, tty.sid):
            log.debug('[{tty.sid}] {event} re-acquiring stale lock, '
                      'previously held by {holder} for {elapsed:0.2f}s '
                      '(stale={stale})'
                      .format(tty=tty, event=event, holder=holder,
                              elapsed=elapsed, stale=stale))
        elif acquired and tap_events:
            log.debug('[{tty.sid}] {event} granted lock.'
                      .format(tty=tty, event=event))
        elif not acquired:
            log.debug('[{tty.sid}] {event} lock rejected; already held '
                      'by active session {holder} for {elapsed:0.2f} seconds '
                      '(stale={stale})'
                      .format(tty=tty, event=event, holder=holder,
                              elapsed=elapsed, stale=stale))
        tty.master_write.send((event, acquired,))

    elif method == 'release':
        if not locks.release(event, tty.sid):
            log.error('[{tty.sid}] {event} lock failed to release, '
                      'not acquired.'.format(tty=tty, event=event))
        elif tap_events:
            log.debug('[{tty.sid}] {event} released lock.'
                      .format(tty=tty, event=event))


def session_recv(terminals, log, tap_events):
    """
    Receive data waiting for terminal sessions.

    All data received from subprocess is handled here.
    """
    for sid, tty in terminals:
        while tty.master_read.poll():
            try:
                event, data = tty.master_read.recv()
            except (EOFError, OSError) as err:
                # sub-process unexpectedly closed
                log.debug('master_read pipe: {0!r}'.format(err))
                kill_session(tty.client, 'master_read pipe: {0!r}'.format(err))
                break
            except (TypeError, pickle.UnpicklingError) as err:
                log.exception('unpickling error: {0}'.format(err))
                break

            # 'exit' event, unregisters client
            if event == 'exit':
                kill_session(tty.client, 'client exit')
                break

            # 'logger' event, prefix log message with handle and IP address
            elif event == 'logger':
                data.msg = ('{data.handle}[{tty.sid}] {data.msg}'
                            .format(data=data, tty=tty))
                log.handle(data)

            # 'output' event, buffer for tcp socket
            elif event == 'output':
                tty.client.send_unicode(ucs=data[0], encoding=data[1])

            # 'remote-disconnect' event, hunt and destroy
            elif event == 'remote-disconnect':
                for _sid, _tty in terminals:
                    # data[0] is 'send-to' address.
                    if data[0] == _sid:
                        kill_session(
                            _tty.client,
                            'remote-disconnect by {0}'.format(sid))
                        break

            # 'route': message passing directly from one session to another
            elif event == 'route':
                if tap_events:
                    log.debug('route {0!r}'.format(data))
                tgt_sid, send_event, send_val = data[0], data[1], data[2:]
                for _sid, _tty in terminals:
                    if tgt_sid == _sid:
                        try:
                            _tty.master_write.send((send_event, send_val))
                        except OSError:
                            # target session has just disconnected.
                            pass
                        break

            # 'global': message broadcasting to all sessions
            elif event == 'global':
                if tap_events:
                    log.debug('broadcast: {data!r}'.format(data=data))
                for _sid, _tty in terminals:
                    if sid != _sid:
                        try:
                            _tty.master_write.send((event, data,))
                        except OSError:
                            # target session has just disconnected.
                            pass

            # 'set-timeout': set user-preferred timeout
            elif event == 'set-timeout':
                if tap_events:
                    log.debug('[{tty.sid}] set-timeout {data}'
                              .format(tty=tty, data=data))
                tty.timeout = data

            # 'db*': access DBProxy API for shared sqlitedict
            elif event.startswith('db'):
                DBHandler(tty.master_write, event, data).start()

            # 'lock': access fine-grained bbs-global locking
            elif event.startswith('lock'):
                handle_lock(LOCKS, tty, event, data, tap_events, log)

            else:
                log.error('[{tty.sid}] unhandled event, data: '
                          '({event}, {data})'
                          .format(tty=tty, event=event, data=data))


def _loop(servers):
    """ Main event loop. Never returns. """
    # pylint: disable=R0912,R0914,R0915
    #         Too many local variables (24/15)
    from x84.bbs.ini import get_ini

    SELECT_POLL = 0.02  # polling time is 20ms

    # WIN32 has no session_fds (multiprocess queues are not polled using
    # select), use a persistently empty set; for WIN32, sessions are always
    # polled for data at every loop.
    WIN32 = sys.platform.lower().startswith('win32')
    session_fds = set()

    log = logging.getLogger('x84.engine')

    if not len(servers):
        raise ValueError("No servers configured for event loop! (ssh, telnet)")

    tap_events = get_ini('session', 'tap_events', getter='getboolean')
    check_ban = get_fail2ban_function()

    while True:
        # shutdown, close & delete inactive clients,
        for server in servers:
            # bbs sessions that are no longer active on the socket
            # level -- send them a 'kill signal'
            for key, client in list(server.clients.items()):
                if not client.is_active():
                    kill_session(client, 'socket shutdown')
                    del server.clients[key]
            # on-connect negotiations that have completed or failed.
            # delete their thread instance from further evaluation
            for thread in [_thread for _thread in server.threads
                           if _thread.stopped][:]:
                server.threads.remove(thread)

        check_r = list()
        for server in servers:
            check_r.append(server.server_socket.fileno())
            check_r.extend(server.client_fds())
        if not WIN32:
            # WIN32's IPC is not done using sockets, so it
            # is not possible to use select.select() on them
            session_fds = get_session_output_fds(servers)
            check_r.extend(session_fds)

        # We'd like to use timeout 'None', but the registration of
        # a new client in terminal.start_process surprises us with new
        # file descriptors for the session i/o.  Unless we loop for
        # additional `session_fds', a connecting client would block.
        try:
            ready_r, _, _ = select.select(check_r, [], [], SELECT_POLL)
        except (OSError, ValueError) as err:
            # more than likely EBADF (9, 'Bad file descriptor'), it would seem
            # the socket we've just decided to poll has just gone bad.
            log.debug('continue after select.error: {0}'.format(err))
            continue

        for fd in ready_r:
            # see if any new tcp connections were made
            server = find_server(servers, fd)
            if server is not None:
                accept(log, server, check_ban)

        # receive new data from tcp clients.
        client_recv(servers, ready_r, log)
        terms = get_terminals()

        # receive new data from session terminals
        if WIN32 or set(session_fds) & set(ready_r):
            try:
                session_recv(terms, log, tap_events)
            except OSError as err:
                # if the ipc closes while we poll, warn and continue
                log.warning(err)

        # send tcp data to clients
        client_send(terms, log)

        # send session data, poll for user-timeout and disconnect them
        session_send(terms)


if __name__ == '__main__':
    sys.exit(main())
