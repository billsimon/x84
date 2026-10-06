"""
SSH Matrix for x/84.

This script is the default session entry point for all ssh connections.

As our transport is ssh -- we've *already* authenticated the user,
though, when 'anonymous' login is enabled, or the handle is one of
the 'new user handles', we pass the mutually exclusive boolean keyword
arguments 'anonymous' or 'new'.

The argument 'username' is always set as the 'user@' argument of the
connecting ssh client -- it could be 'anonymous' or 'new', or any
case insensitive match of a user handle -- it does not necessarily
guarantee that the user exists!

When set, this is a user that should be found under find_user(username)
who may have already authenticated by some various means.
"""


def main(anonymous=False, new=False, username=''):
    """ Main procedure. """
    from x84.bbs import echo, goto, find_user, get_ini, disconnect
    topscript = get_ini('matrix', 'topscript') or 'top'
    nuascript = get_ini('nua', 'script') or 'nua'

    # http://www.termsys.demon.co.uk/vtansi.htm
    # disable line-wrapping
    echo(u'\x1b[7l')

    # http://www.xfree86.org/4.5.0/ctlseqs.html
    # Save xterm icon and window title on stack.
    echo(u'\x1b[22;0t')

    if anonymous:
        # user ssh'd in as anonymous@
        goto(topscript, 'anonymous')
    elif new:
        # user ssh'd in as new@
        goto(nuascript)

    handle = find_user(username)
    if handle is None:
        # the account was deleted after authentication.
        disconnect('no such user: {0!r}'.format(username))
    goto(topscript, handle=handle)
