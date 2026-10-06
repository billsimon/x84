""" Database proxy helper for x/84. """
# std imports
import logging
import time

# local
from x84.bbs.ini import get_ini
from x84.db import (
    get_db_filepath,
    get_database,
    get_db_func,
    get_db_lock_key,
    log_db_cmd,
    thread_holder,
    thread_holder_alive,
    LOCKS,
)

#: time to wait before retrying to acquire a lock held by another.
LOCK_RETRY_INTERVAL = 0.05


class DBProxy(object):

    """
    Provide dictionary-like object interface to shared database.

    A database call, such as __len__() or keys() is issued as a command
    to the main engine when ``use_session`` is True, which spawns a thread
    to acquire a lock on the database and return the results via IPC pipe
    transfer.
    """

    def __init__(self, schema, table='unnamed', use_session=True):
        """
        Class initializer.

        :param str scheme: database key, becomes basename of .sqlite3 file.
        :param str table: optional database table.
        :param bool use_session: Whether iterable returns should be sent over
                                 an IPC pipe (client is a
                                 :class:`x84.bbs.session.Session` instance),
                                 or returned directly (such as used by the main
                                 thread engine components.)
        """
        self.log = logging.getLogger(__name__)
        self.schema = schema
        self.table = table
        self._tap_db = get_ini('session', 'tap_db', getter='getboolean')

        from x84.bbs.session import getsession
        self._session = use_session and getsession()

    def proxy_iter_session(self, method, *args):
        """ Proxy for iterable-return method calls over session IPC pipe. """
        event = 'db={0}'.format(self.schema)
        self._session.flush_event(event)
        self._session.send_event(event, (self.table, method, args))
        data = self._session.read_event(event)
        assert data == (None, 'StartIteration'), (
            'iterable proxy used on non-iterable, {0!r}'.format(data))
        data = self._session.read_event(event)
        while data != (None, StopIteration):
            yield data
            data = self._session.read_event(event)
        self._session.flush_event(event)

    def proxy_method_direct(self, method, *args):
        """ Proxy for direct dictionary method calls. """
        dictdb = get_database(filepath=get_db_filepath(self.schema),
                              table=self.table)
        func = get_db_func(dictdb, method)
        if self._tap_db:
            log_db_cmd(self.log, self.schema, method, args)
        return func(*args)

    def proxy_iter(self, method, *args):
        """ Proxy for iterable dictionary method calls. """
        if self._session:
            return self.proxy_iter_session(method, *args)

        return iter(self.proxy_method_direct(method, *args))

    def proxy_method(self, method, *args):
        """ Proxy for dictionary method calls. """
        if self._session:
            return self.proxy_method_session(method, *args)

        return self.proxy_method_direct(method, *args)

    def proxy_method_session(self, method, *args):
        """ Proxy for dictionary method calls over IPC pipe. """
        event = 'db-{0}'.format(self.schema)
        self._session.send_event(event, (self.table, method, args))
        return self._session.read_event(event)

    def acquire(self):
        """
        Acquire system-wide lock on database (blocking).

        Locks are held by the engine process, so that they are shared by all
        sessions and engine threads (such as the message network poller).
        """
        key = get_db_lock_key(self.schema, self.table)
        if self._tap_db:
            self.log.debug('lock acquire schema=%s, table=%s',
                           self.schema, self.table)
        while True:
            if self._session:
                self._session.send_event(key, ('acquire', None))
                acquired = self._session.read_event(key)
            else:
                acquired, _, _ = LOCKS.acquire(
                    key, thread_holder(), is_alive=thread_holder_alive)
            if acquired:
                return
            time.sleep(LOCK_RETRY_INTERVAL)

    def release(self):
        """ Release system-wide lock on database. """
        key = get_db_lock_key(self.schema, self.table)
        if self._tap_db:
            self.log.debug('lock release schema=%s, table=%s',
                           self.schema, self.table)
        if self._session:
            self._session.send_event(key, ('release', None))
        else:
            LOCKS.release(key, thread_holder())

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.release()

    # pylint: disable=C0111
    #        Missing docstring
    def __contains__(self, key):
        return self.proxy_method('__contains__', key)
    __contains__.__doc__ = dict.__contains__.__doc__

    def __getitem__(self, key):
        return self.proxy_method('__getitem__', key)
    __getitem__.__doc__ = dict.__getitem__.__doc__

    def __setitem__(self, key, value):
        return self.proxy_method('__setitem__', key, value)
    __setitem__.__doc__ = dict.__setitem__.__doc__

    def __delitem__(self, key):
        return self.proxy_method('__delitem__', key)
    __delitem__.__doc__ = dict.__delitem__.__doc__

    def get(self, key, default=None):
        return self.proxy_method('get', key, default)
    get.__doc__ = dict.get.__doc__

    def has_key(self, key):
        """ Deprecated form of ``key in database``. """
        return self.proxy_method('__contains__', key)

    def setdefault(self, key, value):
        return self.proxy_method('setdefault', key, value)
    setdefault.__doc__ = dict.setdefault.__doc__

    def update(self, *args):
        return self.proxy_method('update', *args)
    update.__doc__ = dict.update.__doc__

    def __len__(self):
        return self.proxy_method('__len__')
    __len__.__doc__ = dict.__len__.__doc__

    def values(self):
        return self.proxy_method('values')
    values.__doc__ = dict.values.__doc__

    def items(self):
        return self.proxy_method('items')
    items.__doc__ = dict.items.__doc__

    def iteritems(self):
        """ Return iterator of database (key, value) pairs. """
        return self.proxy_iter('iteritems')

    def iterkeys(self):
        """ Return iterator of database keys. """
        return self.proxy_iter('iterkeys')

    def itervalues(self):
        """ Return iterator of database values. """
        return self.proxy_iter('itervalues')

    def __iter__(self):
        return iter(self.keys())

    def keys(self):
        return self.proxy_method('keys')
    keys.__doc__ = dict.keys.__doc__

    def pop(self, key, *default):
        return self.proxy_method('pop', key, *default)
    pop.__doc__ = dict.pop.__doc__

    def popitem(self):
        return self.proxy_method('popitem')
    popitem.__doc__ = dict.popitem.__doc__

    def copy(self):
        # https://github.com/piskvorky/sqlitedict/issues/20
        # @jquast: should sqlitedict have a .copy() method? "no."
        return dict(self.proxy_method('items'))
    copy.__doc__ = dict.copy.__doc__
