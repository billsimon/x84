""" Tests for the database layer of x/84. """
# std imports
import datetime
import struct
import threading

# 3rd-party
import pytest

# local
from x84.bbs.dbproxy import DBProxy
from x84.db import LockTable, _decode, get_db_func, parse_dbevent


# -- helpers that assemble pickles exactly as python 2 (x84 v2) wrote them.

def py2_str(value):
    """ pickle opcode for a python 2 ``str`` (bytes) of ``value``. """
    assert len(value) < 256
    return b'U' + bytes([len(value)]) + value


def py2_unicode(value):
    """ pickle opcode for a python 2 ``unicode`` of ``value``. """
    encoded = value.encode('utf8')
    return b'X' + struct.pack('<I', len(encoded)) + encoded


def py2_datetime(when):
    """ pickle opcodes for a python 2 :class:`datetime.datetime`. """
    state = (bytes([when.year >> 8, when.year & 0xff, when.month, when.day,
                    when.hour, when.minute, when.second]) +
             when.microsecond.to_bytes(3, 'big'))
    return b'cdatetime\ndatetime\n' + py2_str(state) + b'\x85R'


def py2_user(handle, salt, digest, groups=(), calls=3):
    """ Return pickle of :class:`x84.bbs.userbase.User` as by python 2. """
    groups_pickle = (b'c__builtin__\nset\n]('
                     + b''.join(py2_unicode(grp) for grp in groups)
                     + b'e\x85R')
    return (b'\x80\x02cx84.bbs.userbase\nUser\n)\x81}('
            + py2_str(b'_handle') + py2_unicode(handle)
            + py2_str(b'_password') + py2_str(salt) + py2_str(digest)
            + b'\x86'
            + py2_str(b'_location') + py2_unicode(u'Earth')
            + py2_str(b'_email') + py2_unicode(u'')
            + py2_str(b'_groups') + groups_pickle
            + py2_str(b'_calls') + b'K' + bytes([calls])
            + py2_str(b'_lastcall') + b'G' + struct.pack('>d', 1.5e9)
            + b'ub.')


def test_decode_python2_datetime():
    when = datetime.datetime(2015, 3, 4, 5, 6, 7, 890123)
    assert _decode(b'\x80\x02' + py2_datetime(when) + b'.') == when


def test_decode_python2_user_bcrypt(cfg):
    import bcrypt
    cfg.set('system', 'password_digest', 'bcrypt')
    salt = bcrypt.gensalt(rounds=4)
    digest = bcrypt.hashpw(b'secret', salt)
    user = _decode(py2_user(u'jojo', salt, digest, groups=[u'sysop']))
    assert user.handle == 'jojo'
    assert user.groups == {'sysop'}
    assert user.calls == 3
    assert user.auth(u'secret')
    assert not user.auth(u'wrong')


def test_decode_python2_user_internal_digest(cfg):
    # x84 v2 'internal' digest of password 'secret' and this salt, computed
    # by python 2: sha256 hexdigest of (salt + password), 100,000 times.
    import hashlib
    salt = b'c2FsdHNhbHRzYWx0'
    digest = (salt + b'secret').decode('ascii')
    for _ in range(100000):
        digest = hashlib.sha256(digest.encode('ascii')).hexdigest()
    user = _decode(py2_user(u'jojo', salt, digest.encode('ascii')))
    assert user.auth(u'secret')
    assert not user.auth(u'Secret')


def test_dbproxy_direct(cfg):
    db = DBProxy('testdb', use_session=False)
    db['a'] = 1
    db.update({'b': 2})
    assert 'a' in db
    assert db['a'] == 1
    assert sorted(db.keys()) == ['a', 'b']
    assert sorted(db.items()) == [('a', 1), ('b', 2)]
    assert sorted(db.iterkeys()) == ['a', 'b']
    assert len(db) == 2
    assert db.get('missing', 'default') == 'default'
    assert db.has_key('b')
    assert db.pop('b') == 2
    assert db.pop('b', None) is None
    del db['a']
    assert len(db) == 0
    with pytest.raises(KeyError):
        db['a']


def test_dbproxy_lock_excludes_threads(cfg):
    """ Read-modify-write under the database lock is never interleaved. """
    db = DBProxy('counter', use_session=False)
    db['n'] = 0

    def increment():
        for _ in range(20):
            with DBProxy('counter', use_session=False) as _db:
                _db['n'] = _db['n'] + 1

    threads = [threading.Thread(target=increment) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert db['n'] == 80


def test_get_db_func_refuses_private_methods(cfg):
    from x84.db import get_database, get_db_filepath
    dictdb = get_database(get_db_filepath('testdb'), 'unnamed')
    with pytest.raises(AssertionError):
        get_db_func(dictdb, '__class__')
    with pytest.raises(AssertionError):
        get_db_func(dictdb, 'terminate')
    assert get_db_func(dictdb, 'keys')() == []


def test_parse_dbevent():
    assert parse_dbevent('db-userbase') == (False, 'userbase')
    assert parse_dbevent('db=userbase') == (True, 'userbase')
    with pytest.raises(AssertionError):
        parse_dbevent('db-../etc/passwd')


def test_lock_table():
    locks = LockTable()
    assert locks.acquire('k', 'a') == (True, None, 0)
    acquired, holder, _ = locks.acquire('k', 'b')
    assert (acquired, holder) == (False, 'a')
    # re-entrant: 'a' acquires twice and must release twice.
    assert locks.acquire('k', 'a')[0]
    assert locks.release('k', 'a')
    assert not locks.acquire('k', 'b')[0]
    assert locks.release('k', 'a')
    assert locks.acquire('k', 'b')[0]
    # 'a' cannot release what 'b' holds.
    assert not locks.release('k', 'a')
    # locks of inactive holders are taken.
    assert locks.acquire('k', 'c', is_alive=lambda holder: False)[0]
    assert locks.holder('k') == 'c'
    assert locks.release_all('c') == ['k']
    assert locks.holder('k') is None
