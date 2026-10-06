""" Userbase record database and utility functions for x/84. """
import logging
import hmac
from x84.bbs.dbproxy import DBProxy
from x84.bbs.ini import get_ini

FN_PASSWORD_DIGEST = None
GROUPDB = 'groupbase'
USERDB = 'userbase'


def list_users():
    """
    Returns all user handles.

    :rtype: list
    :returns list of user handles.
    """
    return list(DBProxy(USERDB).keys())


def get_user(handle):
    """
    Returns User record by handle.

    :rtype: User
    :returns: instance of :class:`User`
    """
    return DBProxy(USERDB)[handle]


def find_user(handle):
    """
    Discover and return matching user by ``handle``, case-insensitive.

    :returns: matching handle as str, or None if not found.
    :rtype: None or str.
    """
    for key in DBProxy(USERDB).keys():
        if handle.lower() == key.lower():
            return key


class Group(object):

    """ A simple group record object. """

    def __init__(self, name, members=()):
        """ Class initializer. """
        self._name = name
        self._members = set(members)

    @property
    def name(self):
        """ Name of this group. """
        return self._name

    @name.setter
    def name(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._name = value

    @property
    def members(self):
        """ Members of this group as user handles. """
        return self._members

    def add(self, handle):
        """ Add user to group. """
        log = logging.getLogger(__name__)
        log.info("Group({!r}).add({!r})".format(self.name, handle))
        self._members.add(handle)

    def remove(self, handle):
        """ Remove user from group. """
        log = logging.getLogger(__name__)
        log.info("Group({!r}).remove({!r})".format(self.name, handle))
        self._members.remove(handle)

    def save(self):
        """ Save group record to database. """
        DBProxy(GROUPDB)[self.name] = self

    def delete(self):
        """ Delete group record, enforces referential integrity with Users. """
        udb = DBProxy(USERDB)
        for chk_user in self.members:
            user = udb[chk_user]
            if self.name in user.groups:
                user.group_del(self.name)
                user.save()
        del DBProxy(GROUPDB)[self.name]


class User(object):

    """ A simple user record. """

    def __init__(self, handle=u'anonymous'):
        """ Class initializer. """
        self._handle = handle
        self._password = (None, None)
        self._location = u''
        self._email = u''
        self._groups = set()
        self._calls = 0
        self._lastcall = 0

    @property
    def handle(self):
        """ User handle, also the database key. """
        return self._handle

    @handle.setter
    def handle(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._handle = value

    @property
    def password(self):
        """
        Password in encrypted form as tuple (salt, hash).

        Not generally used directly, but by :meth:`auth`.

        The ``setter`` of this property is provided a password
        in plain-text and encrypts it as given.

        If a password has not yet been set, it is (None, None).
        """
        return self._password

    @password.setter
    def password(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        log = logging.getLogger(__name__)
        if get_ini('system', 'pass_ucase', getter='getboolean'):
            # facebook and mystic storage style, i wouldn't
            # recommend it though.
            self._password = get_digestpw()(value.upper())
        else:
            self._password = get_digestpw()(value)
        log.info("set password for user {!r}.".format(self.handle))

    def auth(self, try_pass):
        """
        Authenticate user with given password, ``try_pass``.

        :rtype: bool
        :returns: whether the password is correct.
        """
        pass_ucase = get_ini('system', 'pass_ucase', getter='getboolean')
        assert isinstance(try_pass, str)
        assert len(try_pass) > 0
        assert self.password != (None, None), ('account is without password')
        salt, digest = (_as_str(value) for value in self.password)
        digestpw = get_digestpw()
        candidates = [try_pass]
        if pass_ucase:
            candidates.append(try_pass.upper())
        return any(hmac.compare_digest(
            digest.encode('utf8'),
            _as_str(digestpw(candidate, salt)[1]).encode('utf8'))
            for candidate in candidates)

    def __setitem__(self, key, value):
        # pylint: disable=C0111,
        #        Missing docstring
        log = logging.getLogger(__name__)
        adb = DBProxy(USERDB, 'attrs')

        if self.handle == 'anonymous':
            log.debug("set attr {!r} not possible for 'anonymous'".format(key))
            return

        with adb:
            if self.handle not in adb:
                adb[self.handle] = dict([(key, value), ])
            else:
                attrs = adb[self.handle]
                attrs.__setitem__(key, value)
                adb[self.handle] = attrs
        log.debug("set attr {!r} for user {!r}.".format(key, self.handle))
    __setitem__.__doc__ = dict.__setitem__.__doc__

    def get(self, key, default=None):
        # pylint: disable=C0111,
        #        Missing docstring
        log = logging.getLogger(__name__)
        adb = DBProxy(USERDB, 'attrs')
        tap_db = get_ini('session', 'tap_db', getter='getboolean')

        attrs = adb.get(self.handle, None)
        if attrs is None:
            if tap_db:
                log.debug('User({!r}).get(key={!r}) returns default={!r}'
                          .format(self.handle, key, default))
            return default

        if key not in attrs:
            if tap_db:
                log.debug('User({!r}.get(key={!r}) returns default={!r}'
                          .format(self.handle, key, default))
            return default

        if tap_db:
            log.debug('User({!r}.get(key={!r}) returns value.'
                      .format(self.handle, key))
        return attrs[key]
    get.__doc__ = dict.get.__doc__

    def __getitem__(self, key):
        # pylint: disable=C0111,
        #        Missing docstring
        return DBProxy(USERDB, 'attrs')[self.handle][key]
    __getitem__.__doc__ = dict.__getitem__.__doc__

    def __delitem__(self, key):
        # pylint: disable=C0111,
        #        Missing docstring
        log = logging.getLogger(__name__)
        uadb = DBProxy(USERDB, 'attrs')
        with uadb:
            # retrieve attributes from uadb,
            attrs = uadb.get(self.handle, {})
            # delete attribute if exists
            if key in attrs:
                attrs.__delitem__(key)
                uadb[self.handle] = attrs
                log.info("User({!r}) delete attr {!r}."
                         .format(self.handle, key))
    __delitem__.__doc__ = dict.__delitem__.__doc__

    @property
    def groups(self):
        """ Set of groups user is a member of (set of strings). """
        return self._groups

    def group_add(self, group):
        """ Add user to group. """
        return self._groups.add(group)

    def group_del(self, group):
        """ Remove user from group. """
        return self._groups.remove(group)

    def save(self):
        """ Save user record to database. """
        log = logging.getLogger(__name__)
        assert isinstance(self._handle, str), ('handle must be str')
        assert len(self._handle) > 0, ('handle must be non-zero length')
        assert (None, None) != self._password, ('password must be set')
        assert self._handle != u'anonymous', ('anonymous may not be saved.')
        udb = DBProxy(USERDB)
        with udb:
            if 0 == len(udb) and self.is_sysop is False:
                log.warning('{!r}: First new user becomes sysop.'
                         .format(self.handle))
                self.group_add(u'sysop')
            is_new = self.handle not in udb
            udb[self.handle] = self
            if is_new:
                log.info("saved new user '%s'.", self.handle)
        adb = DBProxy(USERDB, 'attrs')
        with adb:
            if self.handle not in adb:
                adb[self.handle] = dict()
        self._apply_groups()

    def delete(self):
        """ Remove user from user and group databases. """
        log = logging.getLogger(__name__)
        gdb = DBProxy(GROUPDB)
        with gdb:
            for gname in self._groups:
                group = gdb.get(gname, None)
                if group is not None and self.handle in group.members:
                    group.remove(self.handle)
                    group.save()
        udb = DBProxy(USERDB)
        with udb:
            del udb[self.handle]
        adb = DBProxy(USERDB, 'attrs')
        with adb:
            if self.handle in adb:
                del adb[self.handle]
        log.info("deleted user '%s'.", self.handle)

    @property
    def is_sysop(self):
        """ Whether the user is in the 'sysop' group. """
        return u'sysop' in self._groups

    @property
    def lastcall(self):
        """ Time last called, ``time.time()`` epoch-formatted (float). """
        return self._lastcall

    @lastcall.setter
    def lastcall(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._lastcall = value

    @property
    def calls(self):
        """Legacy, number of times user has 'called' this board."""
        return self._calls

    @calls.setter
    def calls(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._calls = value

    @property
    def location(self):
        """ Legacy, used as a geographical location, group names, etc. """
        return self._location

    @location.setter
    def location(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._location = value

    @property
    def email(self):
        """ E-mail address. May be used for password resets. """
        return self._email

    @email.setter
    def email(self, value):
        # pylint: disable=C0111
        #         Missing docstring
        self._email = value

    def _apply_groups(self):
        """ Enforce referential integrity of user's groups. """
        log = logging.getLogger(__name__)
        gdb = DBProxy(GROUPDB)
        with gdb:
            for chk_grp in self._groups:
                if chk_grp not in gdb:
                    gdb[chk_grp] = Group(chk_grp, set([self.handle]))
                    log.info("created group {!r} for user {!r}."
                             .format(chk_grp, self.handle))
                # ensure membership in existing groups
                group = gdb[chk_grp]
                if self.handle not in group.members:
                    group.add(self.handle)
                    group.save()
            for gname, group in gdb.items():
                if gname not in self._groups and self.handle in group.members:
                    group.remove(self.handle)
                    group.save()


def _as_str(value):
    """
    Return ``value`` as str.

    Password salts and digests are stored as str, but those of databases
    written by x/84 v2 (python 2) may be bytes.
    """
    if isinstance(value, bytes):
        return value.decode('latin-1')
    return value


def _digestpw_bcrypt(password, salt=None):
    """ Password digest using bcrypt (preferred). """
    import bcrypt
    if not salt:
        salt = bcrypt.gensalt()
    if isinstance(salt, str):
        salt = salt.encode('ascii')
    if isinstance(password, str):
        password = password.encode('utf8')
    # bcrypt only considers the first 72 bytes of a password, and recent
    # versions of the bcrypt library raise ValueError for any longer.
    password = password[:72]
    return (salt.decode('ascii'),
            bcrypt.hashpw(password, salt).decode('ascii'))


def _digestpw_internal(password, salt=None):
    """ Password digest using regular python libs (slow). """
    import hashlib
    import base64
    import os
    if not salt:
        salt = base64.b64encode(os.urandom(32)).decode('ascii')
    digest = salt + password
    for _ in range(0, 100000):
        # pylint: disable=E1101
        #         Module 'hashlib' has no 'sha256'
        digest = hashlib.sha256(digest.encode('utf8')).hexdigest()
    return salt, digest


def _digestpw_plaintext(password, salt=None):
    """ No password digest, just store the passwords in plain text. """
    if not salt:
        salt = 'none'
    return salt, password


def get_digestpw():
    """ Returns singleton to password digest routine. """
    global FN_PASSWORD_DIGEST
    if FN_PASSWORD_DIGEST is not None:
        return FN_PASSWORD_DIGEST

    digest_name = get_ini('system', 'password_digest') or 'bcrypt'
    try:
        FN_PASSWORD_DIGEST = {
            'bcrypt': _digestpw_bcrypt,
            'internal': _digestpw_internal,
            'plaintext': _digestpw_plaintext,
        }[digest_name]
    except KeyError:
        raise ValueError('configuration section [system], value '
                         'password_digest: must be one of bcrypt, internal, '
                         'or plaintext, not {0!r}'.format(digest_name))
    return FN_PASSWORD_DIGEST


def check_new_user(username):
    """ Boolean return when username matches ``newcmds`` ini cfg. """
    from x84.bbs import get_ini
    matching = get_ini(section='matrix',
                       key='newcmds',
                       split=True)
    allowed = get_ini(section='nua',
                      key='allow_apply',
                      getter='getboolean')
    return allowed and username in matching


def check_bye_user(username):
    """ Boolean return when username matches ``byecmds`` in ini cfg. """
    from x84.bbs import get_ini
    matching = get_ini(section='matrix', key='byecmds', split=True)
    return matching and username in matching


def check_anonymous_user(username):
    """ Boolean return when user is anonymous and is allowed. """
    from x84.bbs import get_ini
    matching = get_ini(section='matrix',
                       key='anoncmds',
                       split=True)
    allowed = get_ini(section='matrix',
                      key='enable_anonymous',
                      getter='getboolean',
                      split=False)
    return allowed and username in matching


def check_user_password(username, password):
    """ Boolean return when username and password match user record. """
    from x84.bbs import find_user, get_user
    handle = find_user(username)
    if handle is None:
        return False
    user = get_user(handle)
    if user is None or user.password == (None, None):
        return False
    return bool(password) and user.auth(password)


def parse_public_key(user_pubkey):
    """
    Return paramiko key class instance of a user's public key text.

    The text is in the format of an OpenSSH ``authorized_keys`` entry, such
    as ``ssh-ed25519 AAAAC3Nz... user@host``.  Any key type supported by
    paramiko (rsa, ecdsa, ed25519) may be used.

    :raises ValueError: public key text is malformed or unsupported.
    """
    import binascii
    import base64
    import paramiko

    parts = user_pubkey.split()
    if len(parts) >= 2:
        key_msg, key_data = parts[0], parts[1]
    elif len(parts) == 1:
        # when no key-type is specified, assume rsa
        key_msg, key_data = 'ssh-rsa', parts[0]
    else:
        raise ValueError('Malformed public key format: {0!r}'
                         .format(user_pubkey))
    try:
        decoded_keybytes = base64.b64decode(key_data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('Malformed public key encoding: {0!r}'
                         .format(key_data))
    try:
        return paramiko.PKey.from_type_string(key_msg, decoded_keybytes)
    except (paramiko.SSHException, paramiko.pkey.UnknownKeyType,
            ValueError, TypeError) as err:
        raise ValueError('Malformed or unsupported public key {0!r}: {1}'
                         .format(key_msg, err))


def check_user_pubkey(username, public_key):
    """ Boolean return when public_key matches user record. """
    from x84.bbs import find_user, get_user
    log = logging.getLogger(__name__)
    handle = find_user(username)
    if handle is None:
        return False
    user_pubkey = get_user(handle).get('pubkey', False)
    if not user_pubkey:
        log.debug('pubkey authentication by {0!r} but no '
                  'public key on record for the user.'
                  .format(username))
        return False
    try:
        stored_pubkey = parse_public_key(user_pubkey)
    except ValueError as err:
        log.debug('{0} for stored public key of user {1!r}'
                  .format(err, username))
        return False
    return stored_pubkey == public_key
