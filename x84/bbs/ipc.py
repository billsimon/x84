""" Session IPC package for x/84. """
# std imports
import logging

# local
from x84.bbs.session import getsession


def make_root_logger(out_queue):
    """
    Remove and re-address the root logging handler.

    Any existing handlers of the current process are removed and
    the root logger is re-address to send via an IPC output event
    queue.
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.addHandler(IPCLogHandler(out_queue=out_queue))
    # the engine's log handlers filter by level; forward everything.
    root.setLevel(logging.DEBUG)


class IPCLogHandler(logging.Handler):

    """
    Log handler that sends the log up the 'event pipe'.

    This is a rather novel solution that seems overlooked in documentation,
    a forked process must have some method to propagate its logging records
    up through the main process, otherwise they are lost.
    """

    def __init__(self, out_queue):
        """ Constructor method, requires multiprocessing.Pipe. """
        logging.Handler.__init__(self)
        self.oqueue = out_queue

    def emit(self, record):
        """ Emit log record via IPC output queue. """
        try:
            if record.exc_info:
                # a strange side-effect, sets record.exc_text, which is
                # pickled, whereas a traceback object cannot be.
                self.format(record)
                record.exc_info = None
            # arguments are formatted here: they may not be pickleable.
            record.msg = record.getMessage()
            record.args = None
            record.handle = None
            session = getsession()
            if session:
                record.handle = session.user.handle
            self.oqueue.send(('logger', record))
        except (KeyboardInterrupt, SystemExit):
            raise
        except (BrokenPipeError, EOFError):
            # engine has closed our pipe, there is nobody to log to.
            pass
        except Exception:
            self.handleError(record)


class IPCStream(object):

    """
    Connect blessed.Terminal argument 'stream' to 'writer' queue.

    The ``writer`` queue is a ``multiprocessing.Pipe`` whose master-side
    is polled for output in x84.engine.  Only the ``write()`` method of
    this "stream" and ``is_a_tty`` attribute is called or evaluated by
    blessed.Terminal.  The attribute ``is_a_tty`` is mocked as ``True``.
    """

    def __init__(self, writer):
        self.writer = writer
        self.is_a_tty = True

    def flush(self):
        """ Flush stream (does nothing, writes are not buffered). """

    def write(self, ucs, encoding='ascii'):
        """
        Sends unicode text to Pipe.

        Default encoding is 'ascii', which is unset only when used
        with blessings, which rarely writes directly to the stream
        (context managers, such as "with term.location(0, 0):" have
        such side effects).
        """
        # wrap 'ucs' with call to 'str()', so that special str
        # instances such as blessed.formatters.ParameterizingProxyString
        # can be pickled -- as this one in particular contains a local
        # function (lambda) as an attribute -- which would fail:
        # PicklingError: Can't pickle <type 'function'>: attribute
        #                lookup builtins.function failed
        self.writer.send(('output', (str.__str__(ucs), encoding)))
