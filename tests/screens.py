"""
Harness for tests of full-screen scripts of the default board.

An :class:`Engine` thread services a test session's database and lock
events, as the engine does, and collects its output.
"""
# std imports
import importlib.util
import os
import sys
import threading
import time

# 3rd-party
import pytest

import x84

SCRIPT_PATH = os.path.join(os.path.dirname(x84.__file__), 'default')


class LockedPipe(object):

    """ Pipe of the engine side, sent by several threads. """

    def __init__(self, pipe):
        self.pipe, self.lock = pipe, threading.Lock()

    def send(self, obj):
        with self.lock:
            self.pipe.send(obj)


class Engine(threading.Thread):

    """ Services database and lock events of a session, collects output. """

    def __init__(self, harness):
        super().__init__(daemon=True)
        self.harness = harness
        self.pipe = LockedPipe(harness.master_write)
        self.output = []
        self.broadcasts = []
        self.stopped = False

    def keys(self, text):
        self.pipe.send(('input', text.encode('utf8')))

    def run(self):
        import logging
        from x84.db import DBHandler, LOCKS
        from x84.engine import handle_lock

        class TTY(object):
            sid = self.harness.session.sid
            master_write = self.pipe
        log = logging.getLogger('test')
        while not self.stopped:
            if not self.harness.master_read.poll(0.02):
                continue
            event, data = self.harness.master_read.recv()
            if event.startswith('db'):
                DBHandler(self.pipe, event, data).start()
            elif event.startswith('lock'):
                handle_lock(LOCKS, TTY, event, data, False, log)
            elif event == 'output':
                self.output.append(data[0])
            elif event == 'global':
                self.broadcasts.append(data)

    @property
    def text(self):
        return u''.join(self.output)


@pytest.fixture
def engine(session):
    eng = Engine(session)
    eng.start()
    yield eng
    eng.stopped = True
    eng.join()


@pytest.fixture
def script(session):
    """ Return function loading a script of the default board. """
    sys.path.insert(0, SCRIPT_PATH)

    def load(name):
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(SCRIPT_PATH, name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    yield load
    sys.path.remove(SCRIPT_PATH)


@pytest.fixture
def users(session, engine):
    """ Create users biff, marty and doc; the session is marty. """
    from x84.bbs.userbase import User, get_user
    for handle in (u'biff', u'marty', u'doc'):
        user = User(handle)
        user.password = u'password'
        user.save()
    session.session.user = get_user(u'marty')


#: pause longer than the escape key delay of blessed.
PAUSE = 0.5


def run_script(engine, module, keys, **kwargs):
    """
    Run main() of script ``module``, sending ``keys`` as input events.

    A number among keys is a pause, in seconds.  Returns output.
    """
    def send():
        time.sleep(0.2)
        for key in keys:
            if isinstance(key, float):
                time.sleep(key)
            else:
                engine.keys(key)
                time.sleep(0.05)

    sender = threading.Thread(target=send)
    sender.start()
    try:
        module.main(**kwargs)
    finally:
        sender.join()
    time.sleep(0.1)
    return engine.text


def gosub_editor(monkeypatch, module, text, record=None):
    """ Replace the editor gosub of script ``module`` by ``text``. """
    def fake_gosub(name, **kwargs):
        assert name == 'editor'
        if record is not None:
            record.append(kwargs)
        return text
    monkeypatch.setattr(module, 'gosub', fake_gosub)
