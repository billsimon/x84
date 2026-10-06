""" telnet client for x/84 """
# std imports
import codecs
import logging

#: time to block for keyboard input, polling for socket data.
KEY_POLL = 0.015


def main(host, port=None, encoding='cp437'):
    """
    Call script with argument host and optional argument port to connect to a
    telnet server. ctrl-^ to disconnect.
    """
    from x84.bbs import getsession, getterminal, echo
    from x84.bbs.telnet import TelnetClient
    log = logging.getLogger(__name__)

    assert encoding in ('utf8', 'cp437')
    session, term = getsession(), getterminal()
    session.activity = 'connecting to %s' % (host,)
    port = int(port) if port is not None else 23
    telnet_client = TelnetClient(term_type=session.env.get('TERM', 'ansi'),
                                 height=term.height, width=term.width)
    # cp437 bytes are always final, but utf-8 may be received mid-sequence.
    decoder = codecs.getincrementaldecoder(
        'cp437_art' if encoding == 'cp437' else 'utf8')(errors='replace')

    echo(u"\r\n\r\nEscape character is 'ctrl-^.'")
    if not session.user.get('expert', False):
        term.inkey(3)
    echo(u'\r\nTrying %s:%s... ' % (host, port,))
    try:
        telnet_client.open(host, port)
    except OSError as err:
        echo(term.bold_red('\r\n%s\r\n' % (err,)))
        echo(u'\r\n press any key ..')
        term.inkey()
        return

    echo(u'\r\n... ')
    inp = session.read_event('input', timeout=0)
    echo(u'\r\nConnected to %s.' % (host,))
    session.activity = 'connected to %s' % (host,)
    carriage_returned = False
    with term.fullscreen():
        while True:
            try:
                received = telnet_client.read_available()
            except EOFError:
                break
            if received:
                echo(decoder.decode(received))
            if inp is not None:
                if inp == b'\x1e':  # ctrl-^
                    telnet_client.close()
                    echo(u'\r\n' + term.clear_eol + term.normal)
                    break
                elif not carriage_returned and inp in (b'\x0d', b'\x0a'):
                    telnet_client.write(b'\x0d')
                    log.debug('send {!r}'.format(b'\x0d'))
                    carriage_returned = True
                elif carriage_returned and inp in (b'\x0a', b'\x00'):
                    carriage_returned = False
                elif inp:
                    try:
                        telnet_client.write(inp)
                    except OSError:
                        break
                    log.debug('send {!r}'.format(inp))
                    carriage_returned = False
            inp = session.read_event('input', timeout=KEY_POLL)
    telnet_client.close()
    echo(u'\r\nConnection closed.\r\n')
    echo(u''.join(('\r\n\r\n', term.clear_eol, term.normal, 'press any key')))
    echo(u'\x1b[r')  # unset 'set scrolling region', sometimes set by BBS's
    session.flush_event('input')
    term.inkey()
    return
