""" Database request handler for x/84. """
# std imports
import collections.abc
import threading
import logging
import pickle
import time
import os

# 3rd-party
import sqlitedict

#: Lock for :data:`DATABASES` and database file creation.
FILELOCK = threading.Lock()

#: Open database instances, keyed by ``(filepath, table)``.  Databases are
#: opened once and shared by all threads of the engine process:
#: :class:`sqlitedict.SqliteDict` serializes all requests through its own
#: worker thread.
DATABASES = {}

#: Database methods that return lazy iterators, which must be materialized
#: before they may be returned over an IPC pipe.
ITERATOR_METHODS = ('keys', 'values', 'items',
                    'iterkeys', 'itervalues', 'iteritems')

#: Database methods that may be called by :class:`x84.bbs.dbproxy.DBProxy`:
#: only those of a dictionary.  Others, such as ``terminate()``, which
#: deletes the database file, are refused.
ALLOWED_METHODS = ITERATOR_METHODS + (
    '__contains__', '__getitem__', '__setitem__', '__delitem__', '__len__',
    'get', 'update', 'pop', 'popitem', 'setdefault')


def _decode(obj):
    """
    Deserialize a database value.

    Databases written by x/84 v2 (python 2) contain pickled ``str`` objects
    (such as password salts) and :class:`datetime.datetime` instances, which
    python 3 may only unpickle using ``encoding='latin1'``.  This has no
    effect on values pickled by python 3.
    """
    return pickle.loads(bytes(obj), encoding='latin1')


def get_database(filepath, table):
    """ Return :class:`sqlitedict.SqliteDict` instance for given database. """
    key = (filepath, table)
    with FILELOCK:
        dictdb = DATABASES.get(key)
        if dictdb is None:
            # if the bbs is run as root, file ownerships become read-only
            # and db transactions will throw 'read-only database' errors,
            # exit earlier if we know that file permissions are to blame
            check_db(filepath)
            dictdb = sqlitedict.SqliteDict(filename=filepath,
                                           tablename=table,
                                           autocommit=True,
                                           journal_mode='WAL',
                                           decode=_decode)
            DATABASES[key] = dictdb
    return dictdb


def close_databases():
    """ Close all open databases, committing any pending writes. """
    with FILELOCK:
        while DATABASES:
            _, dictdb = DATABASES.popitem()
            dictdb.close()


def check_db(filepath):
    """
    Verify permission access of given database file.

    :raises AssertionError: file or folder is not writable.
    :raises OSError: could not write containing folder.
    """
    db_folder = os.path.dirname(filepath)
    if not os.path.exists(db_folder):
        os.makedirs(db_folder)
    assert os.access(db_folder, os.F_OK | os.R_OK | os.W_OK), (
        'Must have rw access to db_folder:', db_folder)
    if os.path.exists(filepath):
        assert os.access(filepath, os.F_OK | os.R_OK | os.W_OK), (
            'Must have r+w access to db file:', filepath)


def get_db_filepath(schema):
    """ Return filesystem path of given database ``schema``. """
    from x84.bbs.ini import get_ini
    folder = os.path.expanduser(get_ini('system', 'datapath'))
    return os.path.join(folder, '{0}.sqlite3'.format(schema))


def get_db_func(dictdb, cmd):
    """
    Return callable function of method on ``dictdb``.

    Results of methods that return a lazy iterator, such as ``keys()``, are
    returned as a list.  As a compatibility measure for scripts written for
    python 2, ``has_key()`` is supported.

    :raises AssertionError: not a valid method or not callable.
    """
    if cmd == 'has_key':
        cmd = '__contains__'
    assert cmd in ALLOWED_METHODS, (
        '{cmd!r} is not a permitted method'.format(cmd=cmd))
    assert hasattr(dictdb, cmd), (
        "{cmd!r} not a valid method of {db_type!r}"
        .format(cmd=cmd, db_type=type(dictdb)))
    func = getattr(dictdb, cmd)
    assert callable(func), (
        "{cmd!r} not a callable method of {db_type!r}"
        .format(cmd=cmd, db_type=type(dictdb)))
    if cmd in ITERATOR_METHODS:
        return lambda *args: list(func(*args))
    return func


def parse_dbevent(event):
    """
    Parse a database event into ``(iterable, schema)``.

    Called by class initializer, to determine if the event should return
    an iterable, and for what database name (``schema``).

    :rtype: tuple
    """
    assert event[2] in ('-', '='), ('event name must match db[-=]event')
    iterable = event[2] == '='
    schema = event[3:]
    assert schema.replace('_', '').isalnum() and os.path.sep not in schema, (
        'database schema {!r} must be alpha-numeric and not contain {!r}'
        .format(schema, os.path.sep))

    return iterable, schema


def log_db_cmd(log, schema, cmd, args):
    """ Log database command (when tap_db ini option is used). """
    s_args = '()'
    if len(args):
        s_args = '(*{0})'.format(len(args))
    log.debug('{schema}/{cmd}{args}'.format(schema=schema,
                                            cmd=cmd,
                                            args=s_args))


class LockTable(object):

    """
    Table of bbs-wide, re-entrant, named locks.

    Locks are held by a named ``holder``, the session-id of a bbs session,
    or the name of a thread of the engine process (see :func:`thread_holder`).
    All locks are held in the engine process: sessions request locks by IPC
    events (``lock-<name>``), handled by :func:`x84.engine.handle_lock`.
    """

    def __init__(self):
        self._mutex = threading.Lock()
        #: dictionary of ``key: [holder, acquired_time, depth]``
        self._locks = {}

    def acquire(self, key, holder, stale=None, is_alive=None):
        """
        Try to acquire lock ``key`` for ``holder``, without blocking.

        :param str key: name of lock.
        :param str holder: name of lock holder.
        :param float stale: when not ``None``, a lock that has been held by
            another holder for longer than this many seconds is taken.
        :param callable is_alive: function receiving a holder name, returning
            whether it is still active.  Locks of inactive holders are taken.
        :rtype: tuple
        :returns: tuple of ``(acquired, previous_holder, elapsed)``.
        """
        with self._mutex:
            now = time.time()
            if key in self._locks:
                prev_holder, since, depth = self._locks[key]
                elapsed = now - since
                if prev_holder == holder:
                    # re-entrant
                    self._locks[key][2] = depth + 1
                    return True, prev_holder, elapsed
                if (is_alive is not None and not is_alive(prev_holder)
                        or stale is not None and elapsed > stale):
                    self._locks[key] = [holder, now, 1]
                    return True, prev_holder, elapsed
                return False, prev_holder, elapsed
            self._locks[key] = [holder, now, 1]
            return True, None, 0

    def release(self, key, holder):
        """
        Release lock ``key`` held by ``holder``.

        :rtype: bool
        :returns: False if the lock was not held by ``holder``.
        """
        with self._mutex:
            if key not in self._locks or self._locks[key][0] != holder:
                return False
            self._locks[key][2] -= 1
            if self._locks[key][2] == 0:
                del self._locks[key]
            return True

    def release_all(self, holder):
        """
        Release all locks held by ``holder``.

        :rtype: list
        :returns: names of locks released.
        """
        with self._mutex:
            keys = [key for key, (_holder, _, _) in self._locks.items()
                    if _holder == holder]
            for key in keys:
                del self._locks[key]
            return keys

    def holder(self, key):
        """ Return the holder of lock ``key``, or ``None``. """
        with self._mutex:
            return self._locks.get(key, (None,))[0]


#: Singleton of bbs-wide locks, held in the engine process.
LOCKS = LockTable()

#: Prefix of lock holder names of engine threads.
THREAD_HOLDER_PREFIX = 'thread:'


def thread_holder():
    """ Return lock holder name for the current thread. """
    return '{0}{1}'.format(THREAD_HOLDER_PREFIX, threading.get_ident())


def thread_holder_alive(holder):
    """ Whether the thread of lock holder name ``holder`` is still alive. """
    if not holder.startswith(THREAD_HOLDER_PREFIX):
        return False
    ident = int(holder[len(THREAD_HOLDER_PREFIX):])
    return any(thread.ident == ident for thread in threading.enumerate())


def get_db_lock_key(schema, table):
    """ Return name of lock for database ``schema`` and ``table``. """
    return 'lock-db/{0}/{1}'.format(schema, table)


class DBHandler(threading.Thread):

    """
    This handler receives and handles a dictionary-based "database command".

    See complimenting :class:`x84.bbs.dbproxy.DBProxy`, which behaves as a
    dictionary and "packs" command iterables through an IPC event queue which
    is then dispatched by the engine.

    The return values are sent to the session queue with equal 'event' name.
    """

    def __init__(self, queue, event, data):
        """
        Class initializer.

        :param multiprocessing.Pipe queue: parent input end of a tty session
                                           ipc queue (``tty.master_write``).
        :param str event: database schema in form of string ``'db-schema'``
                          or ``'db=schema'``.  When ``'-'`` is used, the result
                          is returned as a single transfer. When ``'='``, an
                          iterable is yielded and the data is transfered via
                          the IPC Queue as a stream.
        :param tuple data: a dict method proxy command sequence in form of
                           ``(table, command, arguments)``.  For example,
                           ``('unnamed', 'pop', 0).
        """
        self.log = logging.getLogger(__name__)
        self.queue, self.event = queue, event
        self.table, self.cmd, self.args = data

        self.iterable, self.schema = parse_dbevent(event)
        self.filepath = get_db_filepath(self.schema)

        from x84.bbs.ini import get_ini
        self._tap_db = self.log.isEnabledFor(logging.DEBUG) and (
            get_ini('session', 'tap_db', getter='getboolean'))

        threading.Thread.__init__(self, daemon=True)

    def run(self):
        """ Execute database command and return results to session queue. """
        try:
            dictdb = get_database(self.filepath, self.table)
            func = get_db_func(dictdb, self.cmd)
            if self._tap_db:
                log_db_cmd(self.log, self.schema, self.cmd, self.args)

            # single value result,
            if not self.iterable:
                result = func(*self.args)
                if isinstance(result, collections.abc.Iterator):
                    result = list(result)
                self.queue.send((self.event, result))

            # iterable value result,
            else:
                self.queue.send((self.event, (None, 'StartIteration'),))
                for item in func(*self.args):
                    self.queue.send((self.event, item,))
                self.queue.send((self.event, (None, StopIteration,),))

        except (BrokenPipeError, EOFError, ConnectionResetError):
            # our pipe/queue has been disconnected (the session has
            # disconnected).
            return

        # pylint: disable=W0703
        #         Catching too general exception
        except Exception as err:
            # Pokemon exception, send to session
            try:
                self.queue.send(('exception', err,))
            except (OSError, EOFError):
                # our pipe/queue has been disconnected (the session has
                # disconnected), heck this might be the cause of our first
                # exception
                return
