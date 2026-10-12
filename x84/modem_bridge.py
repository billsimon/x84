"""
Bridge a modem call to x/84: ``x84-modem-bridge``.

Run by a softmodem for each call it answers, with the call as stdin and
stdout -- for datamodem, by its ``--exec`` option::

    datamodem answer --calls 0 --exec 'x84-modem-bridge --port 6510'

It connects to the modem server of x/84 (see :mod:`x84.modem`), sends one
line describing the call, from the environment the softmodem provides --
``CONNECT``, ``MODEM_RATE``, ``CALLER_ID`` and so on, the names mgetty and
datamodem use -- and then copies bytes untouched in both directions until
either side ends.

Socket buffers are kept small: the modem's own transmit queue already holds
several seconds of output, and anything queued beyond it is output the
caller's keystrokes must wait behind.
"""
# std imports
import argparse
import errno
import json
import os
import select
import socket
import sys

#: environment variables of the softmodem, and the header fields they fill.
ENVIRONMENT = (
    ('MODEM_RATE', 'rate'),
    ('MODEM_MODULATION', 'modulation'),
    ('MODEM_PROTOCOL', 'protocol'),
    ('CONNECT', 'connect'),
    ('CALLER_ID', 'caller_id'),
    ('CALLER_NAME', 'caller_name'),
    ('CALLED_ID', 'called_id'),
)

#: socket buffer size, see module docstring.
SOCKET_BUFFER = 4096

#: largest read from either side at a time.
CHUNK = 1024


def get_parser():
    """ Return :class:`argparse.ArgumentParser` for x84-modem-bridge. """
    parser = argparse.ArgumentParser(
        prog='x84-modem-bridge',
        description='Connect a modem call, on stdin and stdout, to the '
                    'modem server of x/84.')
    parser.add_argument('--host', default='127.0.0.1',
                        help='address of the x/84 modem server '
                             '(default 127.0.0.1)')
    parser.add_argument('--port', type=int, default=6510,
                        help='port of the x/84 modem server (default 6510)')
    parser.add_argument('--secret', default=None,
                        help='value of [modem] secret, if set; preferably '
                             'given as environment value X84_MODEM_SECRET')
    parser.add_argument('--timeout', type=float, default=10.0,
                        help='seconds to wait to connect (default 10)')
    return parser


def make_header(environ, secret=None):
    """ Return header line describing the call, as bytes. """
    header = {field: environ.get(name, '') for name, field in ENVIRONMENT}
    if not header['rate'] and header['connect']:
        # "14400 V.42/V.42bis", for a softmodem that only sets CONNECT.
        header['rate'] = header['connect'].split()[0].split('/')[0]
    if secret:
        header['secret'] = secret
    return json.dumps(header).encode('utf8') + b'\n'


def write_all(fd, data):
    """ Write all of ``data`` to file descriptor ``fd`` (blocking). """
    while data:
        try:
            sent = os.write(fd, data)
        except InterruptedError:
            continue
        data = data[sent:]


def relay(sock, fd_in, fd_out):
    """
    Copy bytes between ``sock`` and ``fd_in``/``fd_out`` until either ends.

    Each direction is written in full before more is read from its source,
    so that a slow reader holds back its writer, all the way through.
    """
    while True:
        ready, _, _ = select.select([sock, fd_in], [], [])
        if sock in ready:
            try:
                data = sock.recv(CHUNK)
            except ConnectionResetError:
                data = b''
            if not data:
                # x/84 has ended the session: hang up.
                return 'the bbs closed the connection'
            try:
                write_all(fd_out, data)
            except OSError as err:
                if err.errno in (errno.EPIPE, errno.EBADF):
                    return 'the modem stopped reading'
                raise
        if fd_in in ready:
            data = os.read(fd_in, CHUNK)
            if not data:
                # the caller has hung up.
                return 'the call ended'
            try:
                sock.sendall(data)
            except (BrokenPipeError, ConnectionResetError):
                return 'the bbs closed the connection'


def main(argv=None):
    """ Command-line entry point. """
    args = get_parser().parse_args(argv)
    secret = args.secret or os.environ.get('X84_MODEM_SECRET')
    try:
        sock = socket.create_connection((args.host, args.port),
                                        timeout=args.timeout)
    except OSError as err:
        sys.stderr.write('x84-modem-bridge: cannot connect to {0}:{1}: {2}\n'
                         .format(args.host, args.port, err))
        return 1
    sock.settimeout(None)
    for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, option, SOCKET_BUFFER)
        except OSError:
            pass
    try:
        sock.sendall(make_header(os.environ, secret))
        reason = relay(sock, sys.stdin.fileno(), sys.stdout.fileno())
    except KeyboardInterrupt:
        reason = 'interrupted'
    finally:
        sock.close()
    sys.stderr.write('x84-modem-bridge: {0}\n'.format(reason))
    return 0


if __name__ == '__main__':
    sys.exit(main())
