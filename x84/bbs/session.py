# -*- coding: utf-8 -*-
""" Session engine for x/84. """

# std imports
import importlib.machinery
import importlib.util
import collections
import traceback
import logging
import pickle
import time
import sys
import os

# local
from x84.bbs.exception import Disconnected, Goto
from x84.bbs.script_def import Script
from x84.bbs.userbase import User
from x84.bbs.ini import get_ini


#: singleton representing the session connected by current process
SESSION = None


def getsession():
    """ Return :class:`Session` instance of current process. """
    return SESSION


def getterminal():
    """ Return :class:`blessed.Terminal` instance of current session. """
    return getsession().terminal


def goto(script_name, *args, **kwargs):
    """ Change bbs script. Does not return. """
    raise Goto(script_name, *args, **kwargs)


def disconnect(reason=u''):
    """ Disconnect session. Does not return. """
    raise Disconnected(reason,)


def getch(timeout=None):
    """
    A deprecated form of getterminal().inkey().

    This is old behavior -- upstream blessed project does the correct
    thing. please use term.inkey() and see the documentation for
    blessed's inkey() method, it **always** returns unicode, never None,
    and definitely never an integer. However some internal UI libraries
    were built upon getch(), and as such, this remains ...
    """
    keystroke = getterminal().inkey(timeout)
    # and this is the purpose for deprecation; old versions used to
    # return None to indicate timeout; but it is more correct to always
    # return strings, so that .lower() and such operations are always
    # successful without conditional 'is None' checks.
    if keystroke == u'':
        return None
    if keystroke.is_sequence:
        return keystroke.code
    return keystroke


def gosub(script, *args, **kwargs):
    """ Call bbs script with optional arguments, Returns value. """
    script = Script(name=script, args=args, kwargs=kwargs)
    return getsession().runscript(script)


class Session(object):

    """ A per-process Session. Begins by the :meth:`run`. """

    _encoding = None
    _decoder = None
    _activity = None
    _user = None

    def __init__(self, terminal, sid, env, child_pipes, kind, addrport,
                 matrix_args, matrix_kwargs):
        # pylint: disable=R0913
        #        Too many arguments
        """ Instantiate a Session.

        Only one session may be instantiated per process.

        :param blessed.Terminal terminal: interactive terminal associated with
                                          this session.
        :param str sid: session identification string
        :param dict env: transport-negotiated environment variables, should
                         contain at least values for TERM and 'encoding'.
        :param tuple child_pipes: tuple of ``(writer, reader)``.
        :param str kind: transport description string (ssh, telnet)
        :param str addrport: transport ip address and port as string
        :param tuple matrix_args: When non-None, a tuple of positional
                                  arguments passed to the matrix script.
        :param dict matrix_kwargs: When non-None, a dictionary of keyword
                                   arguments passed to the matrix script.
        """
        self.log = logging.getLogger(__name__)

        # pylint: disable=W0603
        #        Using the global statement
        # Manage `SESSION' per-process singleton.
        global SESSION
        assert SESSION is None, 'Only one Session per process allowed'
        SESSION = self

        # public attributes
        self.terminal = terminal
        self.sid = sid
        self.env = env
        self.writer, self.reader = child_pipes
        self.kind = kind
        self.addrport = addrport

        # initialize keyboard encoding
        terminal.set_keyboard_decoder(env['encoding'])

        # private attributes
        self._script_stack = [
            # Initialize the "script stack" with the matrix script
            # using the default configuration value of `script' for all
            # connections, but preferring `script_{kind}', where `kind'
            # may be `telnet', `ssh', or any kind of supporting transport.
            Script(
                name=get_ini('matrix', 'script_{self.kind}'.format(self=self)
                             ) or get_ini('matrix', 'script'),
                args=matrix_args,
                kwargs=matrix_kwargs)
        ]
        self._connect_time = time.time()
        self._last_input_time = time.time()
        self._node = None
        self._script_module = []

        # create event buffer
        self._buffer = dict()

    def to_dict(self):
        """ Dictionary describing this session. """
        retval = {
            attr: getattr(self, attr)
            for attr in (
                'last_input_time',
                'connect_time',
                'encoding',
                'activity',
                'idle',
                'node',
                'pid',
                'sid',
            )
        }
        retval.update(dict(
            handle=self.user.handle,
            term_kind=self.terminal.kind,
            term_width=self.terminal.width,
            term_height=self.terminal.height,
            current_script=self.current_script.name,
        ))
        return retval.copy()

    @property
    def duration(self):
        """ Seconds elapsed since connection began (as float). """
        return time.time() - self.connect_time

    @property
    def connect_time(self):
        """ Time of session start (as float). """
        return self._connect_time

    @property
    def last_input_time(self):
        """ Time of last keypress (as epoch, float). """
        return self._last_input_time

    @property
    def idle(self):
        """ Seconds elapsed since last keypress as float.  """
        return time.time() - self.last_input_time

    @property
    def activity(self):
        """ Current session activity.

        This is arbitrarily set by session scripts.

        This also updates xterm titles, and is globally broadcasted
        as a "current activity" in the Who's online script, for example.
        """
        return self._activity or u'<uninitialized>'

    @activity.setter
    def activity(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        if self._activity != value:
            self.log.debug('activity=%s', value)
            self._activity = value

            if (self.terminal.kind or '').startswith(('xterm', 'rxvt')):
                self.write(u'\x1b]2;{0}\x07'.format(value))

    @property
    def user(self):
        """ :class:`User` instance of this session. """
        return self._user or User()

    @user.setter
    def user(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self.log.info("user {!r} -> {!r}".format(self._user, value.handle))
        self._user = value

    @property
    def encoding(self):
        """ Session encoding, both input and output. """
        return self.env.get('encoding', 'utf8')

    @encoding.setter
    def encoding(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        if value != self.encoding:
            self.log.debug('session encoding {0} -> {1}'
                           .format(self.encoding, value))
            self.env['encoding'] = value
            getterminal().set_keyboard_decoder(value)

    @property
    def pid(self):
        """ Process ID of this session (int). """
        return os.getpid()

    @property
    def tap_input(self):
        """ Whether keyboard input should be logged (bool). """
        return get_ini('session', 'tap_input', getter='getboolean')

    @property
    def tap_output(self):
        """ Whether screen output should be logged (bool). """
        return get_ini('session', 'tap_output', getter='getboolean')

    @property
    def show_traceback(self):
        """ Whether traceback errors should be displayed to user (bool). """
        return get_ini('system', 'show_traceback', getter='getboolean')

    @property
    def script_path(self):
        """
        Base filepath folder for all scripts.

        :rtype: list
        """
        scriptpath_dirs = [
            os.path.expanduser(directory) for directory in
            get_ini('system', 'scriptpath', split=True)]

        # ensure all specified folders exist
        for directory in scriptpath_dirs:
            assert os.path.isdir(directory), (
                'configuration section [system], value scriptpath: '
                'not a folder: {!r}'.format(directory))
        return scriptpath_dirs

    @property
    def current_script(self):
        """ The current script being executed. """
        if len(self._script_stack):
            return self._script_stack[-1]
        return None

    @property
    def script_module(self):
        """
        Base python module instance for userland scripts.

        Each folder of the ``scriptpath`` configuration value is placed into
        :data:`sys.path`, so that scripts may import their sibling modules,
        and its ``__init__.py`` is loaded as a module of the folder's name.

        :rtype: list
        """
        if not self._script_module:
            for directory in self.script_path:
                # load default/__init__.py as 'default',
                folder_name = os.path.basename(directory)

                # put it in sys.path for relative imports
                if directory not in sys.path:
                    sys.path.insert(0, directory)

                spec = importlib.util.spec_from_file_location(
                    folder_name, os.path.join(directory, '__init__.py'),
                    submodule_search_locations=[directory])
                scr_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(scr_module)
                self._script_module.append(scr_module)
        return self._script_module

    @property
    def node(self):
        """
        Unique numeric constant for this session.

        This makes it simpler to refer to users who are online, instead
        of by their full session-id (such as telnet-92.32.10.132:57331)
        one can simply refer to node #1, etc..
        """
        if self._node is not None:
            return self._node

        for node in range(1, 65534):
            event = 'lock-%s/%d' % ('node', node)
            self.send_event(event, ('acquire', None))
            data = self.read_event(event)
            if data is True:
                self._node = node
                return self._node

    def __error_recovery(self):
        """ Recover from general exception in script. """
        if 0 != len(self._script_stack):
            # recover from exception
            fault = self._script_stack.pop()
            prefix = u'stop'
            if len(self._script_stack):
                # scripts remaining on the script_stack, resume the script that
                # called us. Make sure your calling script queries for input or
                # some other decision before chaining a gosub(), or you could
                # end up in an infinite loop of gosub() followed by a crash (!)
                resume = self.current_script
                prefix = u'resume {resume.name}'.format(resume=resume)

            # display error to local log handler and to the user,
            msg = (u'{prefix} after general exception in {fault.name}'
                   .format(prefix=prefix, fault=fault))
            self.log.error(msg)
            self.write(u'\r\n\r\n{msg}\r\n'.format(msg=msg))

            # give time for exception to write down the IPC queue before
            # continuing or exiting, esp. exiting, otherwise STOP message
            # is not often fully received to the transport.
            time.sleep(2)

    def run(self):
        """
        Begin main execution of session.

        Scripts manipulate control flow of scripts by raising the
        ``Goto`` exception, or the gosub function.
        """
        self.log.info('Begin session on node %s', self.node)
        try:
            while len(self._script_stack):
                self.log.debug('script_stack is {self._script_stack!r}'
                               .format(self=self))
                try:
                    self.runscript(self._script_stack.pop())
                    continue

                except Goto as goto_script:
                    self.log.debug('goto {0}'.format(goto_script.value))
                    self._script_stack = [goto_script.value]
                    continue

                except Disconnected as err:
                    self.log.info('Disconnected: %s', err)
                    return None

                except Exception:
                    # Pokemon exception, log and Cc: client, then resume.
                    e_type, e_value, e_tb = sys.exc_info()
                    if self.show_traceback:
                        self.write(self.terminal.normal + u'\r\n')

                    terrs = list()
                    for line in traceback.format_tb(e_tb):
                        for subln in line.split('\n'):
                            terrs.append(subln)
                    terrs.extend(
                        traceback.format_exception_only(e_type, e_value))

                    for e_txt in map(str.rstrip, terrs):
                        self.log.error(e_txt)
                        if self.show_traceback:
                            self.write(e_txt + u'\r\n')

                # recover from general exception
                self.__error_recovery()
            self.log.debug('End of script stack.')
        finally:
            self.close()

    def write(self, ucs, encoding=None):
        """ Write unicode data ``ucs`` to terminal. """
        # do not write empty strings
        if not ucs:
            return
        self.terminal.stream.write(ucs, encoding or self.encoding)

        if self.log.isEnabledFor(logging.DEBUG) and self.tap_output:
            self.log.debug('--> {!r}'.format(ucs))

    def flush_event(self, event):
        """
        Flush and return all data buffered for ``event``.

        :param str event: event name.
        :rtype: list
        """
        flushed = list()
        while True:
            data = self.read_event(event, -1)
            if data is None:
                if 0 != len(flushed):
                    self.log.debug('flushed from %s: %r', event, flushed)
                return flushed
            flushed.append(data)
        return flushed

    def buffer_event(self, event, data=None):
        """
        Buffer and handle IPC data keyed by ``event``.

        :param str event: event name.
        :param data: event data.
        :rtype: bool
        :returns: True if the event was internally handled, and the caller
                  should take no further action.

        Methods internally handled by this method:

        - ``global``: events where the first index of ``data`` is ``AYT``.
          This is sent by other sessions using the ``broadcast`` event, to
          discover "who is online".  All other ``global`` events are buffered
          as an event named by the first index of ``data``.

        - ``info-req``: Where the first data value is the remote session-id
          that requested it, expecting a return value event of ``info-ack``
          whose data values is a dictionary describing a session.  This is
          an extension of the "who is online" event described above.

        - ``gosub``: Allows one session to send another to a different script,
          this is used by the default board ``chat.py`` for a chat request.
        """
        # exceptions aren't buffered; they are thrown!
        if event == 'exception':
            # pylint: disable=E0702
            #        Raising NoneType while only classes, (..) allowed
            raise data

        # these callback-responsive session events should be handled by
        # another method, or by a configurable 'event: callback' registration
        # system.
        # respond to global 'AYT' requests
        if event == 'global' and data[0] == 'AYT':
            reply_to = data[1]
            self.send_event('route', (
                reply_to, 'ACK',
                self.sid, self.user.handle,))
            return True

        # all other global broadcasts, such as ('newmsg', msg_idx), are
        # buffered as an event of the name of its first value, ('newmsg'),
        # with data of its remaining value(s) (msg_idx).
        is_broadcast = False
        if event == 'global':
            is_broadcast = True
            event, data = data[0], (data[1] if len(data) == 2 else data[1:])

        # accept 'gosub' as a literal command to run a new script directly
        # from this buffer_event method.  I'm sure it's fine ...
        if event == 'gosub':
            save_activity = self.activity
            self.log.info('event-driven gosub: {0}'.format(data))
            try:
                self.runscript(Script(*data))
            finally:
                self.activity = save_activity
                # RECURSIVE: we call buffer_event to push-in a duplicate
                # "resize" event, so the script that was interrupted has
                # an opportunity to adjust to the new terminal dimensions
                # (if any), or in the case of subpar clients such as netrunner
                # and syncterm that do not handle "alt screen" correctly,
                # it forces a refresh in the interrupted script, though there
                # aren't any guarantees.
                # Otherwise, it is fine to not require the calling function to
                # refresh -- so long as the target script makes sure(!) to
                # use the "with term.fullscreen()" context manager.
                data = ('resize', (self.terminal.width, self.terminal.height,))
                self.buffer_event('refresh', data)
            return True

        # respond to 'info-req' events by returning pickled session info
        if event == 'info-req':
            self.send_event('route', (
                # data[0] is the session-id to return the reply to.
                data[0], 'info-ack', self.sid, self.to_dict()))
            return True

        if event not in self._buffer:
            # " Once a bounded length deque is full, when new items are added,
            # a corresponding number of items are discarded from the opposite
            # end." -- global events are meant to be disregarded, so a much
            # shorter queue length is used. only the foremost refresh event is
            # important in the case of screen resize.
            self._buffer[event] = collections.deque(
                maxlen=128 if is_broadcast else {
                    'refresh': 1,
                }.get(event, 65534))

        # buffer input
        if event == 'input':
            self.buffer_input(data)
            return False

        # buffer only 1 most recent 'refresh' event
        if event == 'refresh':
            if data[0] == 'resize':
                # inherit terminal dimensions values
                (self.terminal._columns, self.terminal._rows) = data[1]

        # buffer all else
        self._buffer[event].appendleft(data)

        return False

    def buffer_input(self, data, pushback=False):
        """
        Receive keyboard input ,``data``, into ``input`` buffer.

        Updates idle time, buffering raw bytes received from telnet client via
        event queue.  Sometimes a script may poll for, and receive keyboard
        data, but wants to push it back in to the top of the stack to be
        decoded by a later call to term.inkey(); in such case, ``pushback``
        should be set.

        :param bytes data: keyboard input data.
        :param bool pushback: whether it should be pushed to front of stack.
        """
        self._last_input_time = time.time()

        if self.log.isEnabledFor(logging.DEBUG) and self.tap_input:
            self.log.debug('<-- {!r}'.format(data))

        if 'input' not in self._buffer:
            # a rare scenario: inkey() causes a first-event of 'input' to
            # be received without buffering from read_events(); then wants
            # to push it back.  Here, too, we must check and construct the
            # input buffer.  It wouldn't be bad to do this on __init__,
            # either.
            self._buffer['input'] = collections.deque(maxlen=65534)
        # like all other event buffers, data is popped from the right-hand
        # side of the deque: new data is queued at the left, and data that
        # is "pushed back" is placed at the right, to be received next.
        if pushback:
            self._buffer['input'].append(data)
        else:
            self._buffer['input'].appendleft(data)

    def send_event(self, event, data):
        """
        Send data to IPC output queue in form of (event, data).

        Supported ``event`` strings:

        - ``disconnect``: Session wishes to disconnect.

        - ``logger``: Data is logging record, used by IPCLogHandler.

        - ``output``: Unicode data to write to client.

        - ``global``: Broadcast event to other sessions.

        - ``route``: Send an event to another session.

        - ``db-<schema>``: Request sqlite dict method result.

        - ``db=<schema>``: Request sqlite dict method result as iterable.

        - ``lock-<name>``: Fine-grained global bbs locking.

        :param str event: event name.
        :param data: event data.
        :raises Disconnected: the engine has closed the IPC pipe.
        """
        try:
            self.writer.send((event, data))
        except (BrokenPipeError, EOFError, ConnectionResetError) as err:
            raise Disconnected('IPC pipe closed: {0}'.format(err))

    def poll_event(self, event):
        """
        Non-blocking poll for session event.

        :param str event: an IPC event queue by name, such as ``input``.
        :returns: first matching IPC event data, or ``None``.
        """
        return self.read_event(event, timeout=-1)

    def read_event(self, event, timeout=None):
        """
        Return data for given ``event`` by timeout.

        :param str event: an IPC event queue by name, such as ``input``.
        :param int timeout: Value of ``None`` is blocking (default), ``-1`` is
                            non-blocking poll. All other values are blocking up
                            to value of timeout.
        :returns: first matching IPC event data.  If timeout is specified and
                  no matching IPC event is discovered, ``None`` is returned.
        """
        return self.read_events(events=(event,), timeout=timeout)[1]

    def read_events(self, events, timeout=None):
        """
        Return the first matched IPC data for any event specified by timeout.

        :param tuple events: events to search for, for example
                             ``('input', 'refresh')``.
        :param int timeout: Value of ``None`` is blocking (default), ``-1`` is
                            non-blocking poll. All other values are blocking up
                            to value of timeout.
        :rtype: tuple
        :returns: first matching IPC ``event, data`` tuple, where ``event``
                  matches one of the given ``events``.  If timeout is specified
                  and no matching IPC event is discovered, ``(None, None)`` is
                  returned.
        """
        event, data = self._pop_event_buffer(events)
        if event:
            return (event, data)

        if timeout is not None and timeout <= 0:
            # non-blocking: receive all events immediately available.
            while self._recv_event(timeout=0):
                event, data = self._pop_event_buffer(events)
                if event is not None:
                    return (event, data)
            return (None, None)

        timeleft = lambda cmp_time: (
            None if timeout is None else
            timeout if timeout < 0 else
            timeout - (time.time() - cmp_time))

        # begin scanning for matching `events' up to timeout.
        stime = time.time()
        waitfor = timeleft(stime)
        while waitfor is None or waitfor > 0:
            # ask engine process for new event data,
            poll = min(0.5, waitfor) if waitfor is not None else 0.5
            self._recv_event(timeout=poll)
            # an event handled by buffer_event() may have side-effects, such
            # as an event-driven 'gosub', which may itself buffer events.
            event, data = self._pop_event_buffer(events)
            if event is not None:
                return (event, data)
            waitfor = timeleft(stime)
        return (None, None)

    def _recv_event(self, timeout):
        """
        Receive and buffer one event from the engine, up to ``timeout``.

        It is necessary to always buffer an event, as some side-effects may
        occur by doing so (see :meth:`buffer_event`).

        :rtype: bool
        :returns: whether an event was received.
        :raises Disconnected: the engine has closed the IPC pipe.
        """
        try:
            if not self.reader.poll(timeout):
                return False
            event, data = self.reader.recv()
        except pickle.UnpicklingError as err:
            self.log.error(err)
            disconnect(reason='{0}'.format(err))
        except (EOFError, OSError) as err:
            # the engine has closed our pipe, the client is gone.
            disconnect(reason='IPC pipe closed: {0}'.format(err))
        self.buffer_event(event, data)
        return True

    def _pop_event_buffer(self, events):
        """
        Return immediately any event-data already buffered.

        :rtype: tuple
        """
        return next(
            ((_event, self._buffer[_event].pop())
             for _event in events
             if len(self._buffer.get(_event, []))),
            (None, None))

    def runscript(self, script):
        """
        Execute the main() callable of script identified by ``script``.

        :param Script script: target script to execute.
        :returns: the return value of the given script's ``main()`` function.
        """
        self.log.info("runscript {0!r}".format(script.name))
        self._script_stack.append(script)

        # if given a script name such as 'extras.target', adjust the lookup
        # path to be extended by {default_scriptdir}/extras, and adjust
        # script_name to be just 'target'.
        script_relpath = [directory.__path__[0]
                          for directory in self.script_module]
        lookup_paths = script_relpath[:]

        if '.' not in script.name:
            script_name = script.name
        else:
            # build another system path, relative to `script_module'
            remaining, script_name = script.name.rsplit('.', 1)
            for dir_relpath in script_relpath:
                _lookup_path = os.path.join(dir_relpath, *remaining.split('.'))
                lookup_paths.append(_lookup_path)
        module = self._load_script(script_name, lookup_paths)

        # ensure main() function exists!
        if not hasattr(module, 'main'):
            raise RuntimeError("script {0}, module {1}: main() not found."
                               .format(script_name, module))
        if not callable(module.main):
            raise RuntimeError("script {0}, module {1}: {2} not callable."
                               .format(script_name, module, module.main))

        # capture the return value of the script and return
        # to the caller -- so value = gosub('my_game') can retrieve
        # the return value of its main() function.
        value = module.main(*script.args, **script.kwargs)

        # remove the current script from the script stack, since it has
        # finished executing.
        self._script_stack.pop()

        return value

    @staticmethod
    def _load_script(script_name, lookup_paths):
        """
        Load and return a fresh module instance of script by name.

        Scripts are (re-)loaded from source on each call, so that changes
        made to scripts take effect without restarting the server, and so
        that any module-level configuration values are re-evaluated.

        :raises ImportError: script was not found.
        """
        spec = importlib.machinery.PathFinder.find_spec(
            script_name, lookup_paths)
        if spec is None:
            raise ImportError('script {0!r} not found in {1!r}'.format(
                script_name, lookup_paths), name=script_name)
        module = importlib.util.module_from_spec(spec)
        # registered in sys.modules, just as an import statement does, so
        # that the script's own import statements of it (such as profile.py
        # importing nua.py) find this same instance.
        sys.modules[script_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(script_name, None)
            raise
        return module

    def close(self):
        """ Close session, currently releases ``node`` lock.. """
        if self._node is not None:
            try:
                self.send_event(
                    event='lock-node/%d' % (self._node),
                    data=('release', None))
            except (BrokenPipeError, EOFError, OSError):
                # engine has already closed our pipe.
                pass
