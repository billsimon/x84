"""
Telnet protocol constants and a minimal telnet client for x/84.

The standard library module ``telnetlib`` was removed in Python 3.13; the
protocol constants (RFC 854 and friends) are defined here as single-byte
:class:`bytes`, and :class:`TelnetClient` provides just enough of a client
for the default board's ``telnet.py`` script to connect to other systems.
"""
# std imports
import logging
import select
import socket
import struct


def _byte(value):
    """ Return single-byte :class:`bytes` of integer ``value``. """
    return bytes((value,))


# telnet commands
IAC = _byte(255)    # "Interpret As Command"
DONT = _byte(254)
DO = _byte(253)
WONT = _byte(252)
WILL = _byte(251)
SB = _byte(250)     # Subnegotiation Begin
GA = _byte(249)     # Go Ahead
EL = _byte(248)     # Erase Line
EC = _byte(247)     # Erase Character
AYT = _byte(246)    # Are You There
AO = _byte(245)     # Abort Output
IP = _byte(244)     # Interrupt Process
BRK = _byte(243)    # Break
DM = _byte(242)     # Data Mark
NOP = _byte(241)    # No Operation
SE = _byte(240)     # Subnegotiation End

# telnet options
BINARY = _byte(0)           # rfc856
ECHO = _byte(1)             # rfc857
SGA = _byte(3)              # rfc858, suppress go ahead
STATUS = _byte(5)           # rfc859
TTYPE = _byte(24)           # rfc1091, terminal type
NAWS = _byte(31)            # rfc1073, window size
TSPEED = _byte(32)          # rfc1079, terminal speed
LFLOW = _byte(33)           # rfc1372, remote flow control
LINEMODE = _byte(34)        # rfc1184
XDISPLOC = _byte(35)        # rfc1096, X display location
AUTHENTICATION = _byte(37)  # rfc2941
ENCRYPT = _byte(38)         # rfc2946
NEW_ENVIRON = _byte(39)     # rfc1572
CHARSET = _byte(42)         # rfc2066

# sub-negotiation commands
IS = _byte(0)
SEND = _byte(1)


class TelnetClient(object):

    """
    A minimal, non-blocking telnet client.

    Negotiates only what is necessary for a BBS-to-BBS connection: echo and
    suppress-go-ahead are accepted from the server, and terminal type and
    window size are offered when asked; all else is refused.
    """

    #: maximum bytes received per call to :meth:`read_available`.
    BLOCKSIZE = 4096

    def __init__(self, term_type='ansi', width=80, height=24):
        """
        Class initializer.

        :param str term_type: terminal type reported to the remote server.
        :param int width: terminal width reported to the remote server.
        :param int height: terminal height reported to the remote server.
        """
        self.log = logging.getLogger(__name__)
        self.term_type = term_type
        self.width = width
        self.height = height
        self.sock = None
        self.eof = False
        self._iac_state = None
        self._iac_cmd = None
        self._sb_buffer = bytearray()

    def open(self, host, port=23, timeout=10):
        """ Connect to ``host`` and ``port``. """
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setblocking(False)

    def close(self):
        """ Close connection. """
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
                self.eof = True

    def fileno(self):
        """ File descriptor of socket. """
        return self.sock.fileno()

    def write(self, data):
        """ Send bytes ``data``, escaping any IAC bytes. """
        self.sock.sendall(data.replace(IAC, IAC + IAC))

    def _send_command(self, *parts):
        """ Send raw telnet command bytes. """
        self.sock.sendall(b''.join(parts))

    def read_available(self, timeout=0):
        """
        Return bytes received up to ``timeout``, with telnet commands removed.

        :raises EOFError: connection was closed by the remote end.
        """
        if self.eof or self.sock is None:
            raise EOFError('connection closed')
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return b''
        try:
            data = self.sock.recv(self.BLOCKSIZE)
        except BlockingIOError:
            return b''
        except OSError as err:
            self.eof = True
            raise EOFError(str(err))
        if not data:
            self.eof = True
            raise EOFError('connection closed by remote end')
        return self._process(data)

    def _process(self, data):
        """ Process received ``data``, returning only non-command bytes. """
        result = bytearray()
        for idx in range(len(data)):
            byte = data[idx:idx + 1]
            if self._iac_state is None:
                if byte == IAC:
                    self._iac_state = 'iac'
                else:
                    result.extend(byte)
            elif self._iac_state == 'iac':
                if byte == IAC:
                    result.extend(byte)
                    self._iac_state = None
                elif byte in (DO, DONT, WILL, WONT):
                    self._iac_cmd = byte
                    self._iac_state = 'option'
                elif byte == SB:
                    self._sb_buffer = bytearray()
                    self._iac_state = 'sb'
                else:
                    # two-byte commands (NOP, GA, etc.) are ignored.
                    self._iac_state = None
            elif self._iac_state == 'option':
                self._handle_option(self._iac_cmd, byte)
                self._iac_state = None
            elif self._iac_state == 'sb':
                if byte == IAC:
                    self._iac_state = 'sb-iac'
                else:
                    self._sb_buffer.extend(byte)
            elif self._iac_state == 'sb-iac':
                if byte == SE:
                    self._handle_sb(bytes(self._sb_buffer))
                    self._iac_state = None
                else:
                    # escaped IAC within sub-negotiation
                    self._sb_buffer.extend(byte)
                    self._iac_state = 'sb'
        return bytes(result)

    def _handle_option(self, cmd, opt):
        """ Reply to a three-byte option negotiation. """
        if cmd == WILL:
            reply = DO if opt in (ECHO, SGA, BINARY) else DONT
            self._send_command(IAC, reply, opt)
        elif cmd == DO:
            if opt in (TTYPE, SGA, BINARY):
                self._send_command(IAC, WILL, opt)
            elif opt == NAWS:
                self._send_command(IAC, WILL, opt)
                self.send_naws()
            else:
                self._send_command(IAC, WONT, opt)
        # DONT and WONT require no reply for options we never enabled.

    def _handle_sb(self, buf):
        """ Reply to sub-negotiation ``buf``. """
        if buf[:2] == TTYPE + SEND:
            self._send_command(IAC, SB, TTYPE, IS,
                               self.term_type.encode('ascii', 'replace'),
                               IAC, SE)

    def send_naws(self):
        """ Send window size (NAWS) sub-negotiation. """
        size = struct.pack('!HH', self.width, self.height)
        self._send_command(IAC, SB, NAWS, size.replace(IAC, IAC + IAC),
                           IAC, SE)
