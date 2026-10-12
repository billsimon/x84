"""
Modem matrix (login) script for x/84.

This script is the session entry point for dial-in modem callers (see
:mod:`x84.modem`), configured by option ``script_modem`` of section
``[matrix]``.  It tells the caller what they connected at, then hands over
to the ordinary login, ``script`` of section ``[matrix]``.

The session environment describes the call:

- ``CONNECT``: the modem's result code, such as ``14400 V.42/V.42bis``.
- ``MODEM_RATE``: bit rate, such as ``14400``.
- ``MODEM_MODULATION``, ``MODEM_PROTOCOL``: such as ``v32bis`` and ``V.42``.
- ``CALLER_ID``, ``CALLER_NAME``, ``CALLED_ID``: as the telephone network
  provided them, which may be empty or untrue.
"""


def main():
    """ Main procedure. """
    from x84.bbs import getsession, getterminal, echo, goto, get_ini
    from x84.bbs.output import slow_modem_rate

    session, term = getsession(), getterminal()
    session.activity = u'dialling in'
    bbsname = get_ini('system', 'bbsname') or 'x/84'
    connect = session.env.get('CONNECT') or session.env.get('MODEM_RATE')

    echo(term.normal + u'\r\n')
    echo(u'{bbsname}, node {node}{connect}\r\n'.format(
        bbsname=term.bold(bbsname), node=session.node,
        connect=u', CONNECT {0}'.format(connect) if connect else u''))
    if slow_modem_rate():
        echo(term.bold_black(u'Artwork is skipped at this speed.\r\n'))
    echo(u'\r\n')

    goto(get_ini('matrix', 'script') or 'matrix')
