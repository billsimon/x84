""" Package provides base server for x/84. """
import logging
import socket


class BaseServer(object):

    """ Base class for server implementations. """

    #: Maximum number of clients
    MAX_CONNECTIONS = 100

    #: Number of clients that can wait to be accepted
    LISTEN_BACKLOG = 5

    #: Client factory should be a class defining what should be instantiated
    #: for the client instance.
    client_factory = None

    #: Connect factory should be a class, derived from threading.Thread, that
    #: should be instantiated on-connect to perform negotiation and launch the
    #: bbs session upon success.
    connect_factory = None

    #: Configuration section name containing ``addr`` and ``port`` options.
    config_section = None

    #: Default port when not configured.
    default_port = None

    def __init__(self, config):
        """
        Class initializer: bind and listen on configured address and port.

        :param configparser.ConfigParser config: bbs configuration, the
            section named by :attr:`config_section` should contain options
            ``'addr'`` and ``'port'``.
        :raises SystemExit: the address could not be bound.
        """
        self.log = logging.getLogger(self.__class__.__module__)
        self.config = config

        #: Dictionary of active clients, (file descriptor, Client, ...).
        #: These are per-instance: each server tracks only its own clients.
        self.clients = {}

        #: List of on-connect negotiating threads.
        self.threads = []

        section = self.config_section
        self.address = config.get(section, 'addr')
        self.port = self.default_port
        if config.has_option(section, 'port'):
            self.port = config.getint(section, 'port')
        self.server_socket = self.bind(self.address, self.port)
        self.log.info('{section} listening on {self.address}:{self.port}/tcp'
                      .format(section=section, self=self))

    def bind(self, address, port):
        """ Return a listening socket bound to ``(address, port)``. """
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        server_socket = socket.socket(family, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_socket.bind((address, port))
            server_socket.listen(self.LISTEN_BACKLOG)
        except OSError as err:
            self.log.error('Unable to bind {0}:{1}: {2}'
                           .format(address, port, err))
            server_socket.close()
            raise SystemExit(1)
        return server_socket

    @classmethod
    def client_factory_kwargs(cls, instance):
        """
        Return keyword arguments for the client_factory.

        Method should be derived and modified, A dictionary may be substituted.
        The default return value is an empty dictionary.

        :rtype dict
        """
        # pylint: disable=W0613
        #         Unused argument 'instance'
        return dict()

    @classmethod
    def connect_factory_kwargs(cls, instance):
        """
        Return keyword arguments for the connect_factory.

        Method should be derived and modified, A dictionary may be substituted.
        The default return value is an empty dictionary.

        :rtype dict
        """
        # pylint: disable=W0613
        #         Unused argument 'instance'
        return dict()

    def client_count(self):
        """ Return number of active connections.  """
        return len(self.clients)

    def client_list(self):
        """ Return list of connected clients. """
        return list(self.clients.values())

    def client_fds(self):
        """ Return list of client file descriptors.  """
        return [fd for fd in (client.fileno()
                              for client in self.clients.values())
                if fd is not None and fd >= 0]

    def clients_ready(self, ready_fds=None):
        """
        Return list of clients with data ready to be receive.

        :param list ready_fds: file descriptors already known to be ready
        """
        if ready_fds is None:
            # given no file descriptors, we must iterate them all by hand.
            return [client for client in list(self.clients.values())
                    if client.recv_ready()]

        # given a list of ready_fds pairs, we return only clients with
        # matching file descriptors.
        return [client for client in list(self.clients.values())
                if client.fileno() in ready_fds]
