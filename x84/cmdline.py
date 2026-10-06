""" Command-line parser for x/84. """
import argparse
import sys
import os


def get_parser():
    """ Return :class:`argparse.ArgumentParser` for the x84 command. """
    from x84 import __version__
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or 'x84',
        description='x/84 telnet, ssh and rlogin BBS server.')
    parser.add_argument('--config', metavar='FILEPATH',
                        help='location of bbs configuration file '
                             '(default: /etc/x84/default.ini, then '
                             '~/.x84/default.ini)')
    parser.add_argument('--logger', metavar='FILEPATH',
                        help='location of logging configuration file '
                             '(default: /etc/x84/logging.ini, then '
                             '~/.x84/logging.ini)')
    parser.add_argument('--version', action='version',
                        version='%(prog)s {0}'.format(__version__))
    return parser


def parse_args(argv=None):
    """
    Parse system arguments and return lookup path for bbs and log ini.

    :param list argv: arguments to parse, ``sys.argv[1:]`` by default.
    :rtype: tuple
    :returns: tuple of ``(lookup_bbs, lookup_log)``, each a tuple of
              file paths in order of preference.
    """
    if sys.platform.lower().startswith('win32'):
        system_path = os.path.join('C:', 'x84')
    else:
        system_path = os.path.join(os.path.sep, 'etc', 'x84')

    lookup_bbs = (os.path.join(system_path, 'default.ini'),
                  os.path.expanduser(os.path.join('~', '.x84', 'default.ini')))

    lookup_log = (os.path.join(system_path, 'logging.ini'),
                  os.path.expanduser(os.path.join('~', '.x84', 'logging.ini')))

    args = get_parser().parse_args(argv)
    if args.config:
        lookup_bbs = (args.config,)
    if args.logger:
        lookup_log = (args.logger,)
    return (lookup_bbs, lookup_log)
