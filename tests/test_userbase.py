""" Tests for the user database of x/84. """
# 3rd-party
import pytest

# local
from x84.bbs.userbase import (
    User, Group, get_user, find_user, list_users, check_user_password,
    check_user_pubkey, parse_public_key, check_new_user, check_bye_user,
    check_anonymous_user,
)


def make_user(handle, password='password'):
    user = User(handle)
    user.password = password
    user.save()
    return user


def test_first_user_is_sysop(cfg):
    first = make_user(u'first')
    second = make_user(u'second')
    assert first.is_sysop
    assert not second.is_sysop
    assert get_user(u'first').is_sysop
    assert u'first' in Group.__init__.__globals__['DBProxy']('groupbase')[
        u'sysop'].members


def test_find_user_case_insensitive(cfg):
    make_user(u'Jojo')
    assert find_user(u'jOJO') == u'Jojo'
    assert find_user(u'nobody') is None
    assert list_users() == [u'Jojo']


def test_password_auth(cfg):
    make_user(u'jojo', u'correct horse')
    assert check_user_password(u'JOJO', u'correct horse')
    assert not check_user_password(u'jojo', u'wrong')
    assert not check_user_password(u'jojo', u'')
    assert not check_user_password(u'nobody', u'correct horse')


def test_bcrypt_password(cfg):
    cfg.set('system', 'password_digest', 'bcrypt')
    user = User(u'jojo')
    user.password = u'p\xe4ssw\xf6rd'
    salt, digest = user.password
    assert isinstance(salt, str) and isinstance(digest, str)
    assert digest.startswith('$2')
    assert user.auth(u'p\xe4ssw\xf6rd')
    assert not user.auth(u'password')


def test_long_bcrypt_password(cfg):
    # bcrypt considers only the first 72 bytes, newer versions of the bcrypt
    # library raise ValueError for any longer, which we must not.
    cfg.set('system', 'password_digest', 'bcrypt')
    user = User(u'jojo')
    user.password = u'x' * 100
    assert user.auth(u'x' * 100)


def test_user_attributes(cfg):
    user = make_user(u'jojo')
    user['color'] = 'red'
    assert get_user(u'jojo').get('color') == 'red'
    assert user.get('missing', 42) == 42
    del user['color']
    assert user.get('color') is None
    # anonymous users may not store attributes
    anon = User()
    anon['color'] = 'blue'
    assert anon.get('color') is None


def test_user_delete_removes_attributes(cfg):
    user = make_user(u'jojo')
    make_user(u'other')
    user['secret'] = 'value'
    user.delete()
    assert find_user(u'jojo') is None
    # a new account of the same name must not inherit attributes.
    reborn = make_user(u'jojo')
    assert reborn.get('secret') is None


def test_unknown_digest_raises(cfg):
    cfg.set('system', 'password_digest', 'md5')
    with pytest.raises(ValueError):
        User(u'jojo').password = u'x'


@pytest.mark.parametrize('key_type', ['ed25519', 'rsa', 'ecdsa'])
def test_public_key_auth(cfg, key_type):
    import paramiko
    if key_type == 'rsa':
        key = paramiko.RSAKey.generate(2048)
    elif key_type == 'ecdsa':
        key = paramiko.ECDSAKey.generate()
    else:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
        import io
        private = ed25519.Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption()).decode('ascii')
        key = paramiko.Ed25519Key(file_obj=io.StringIO(pem))
    pubkey_text = u'{0} {1} user@host'.format(key.get_name(), key.get_base64())
    assert parse_public_key(pubkey_text) == key

    user = make_user(u'jojo')
    user['pubkey'] = pubkey_text
    assert check_user_pubkey(u'jojo', key)
    other = paramiko.RSAKey.generate(2048)
    assert not check_user_pubkey(u'jojo', other)


def test_parse_public_key_malformed():
    with pytest.raises(ValueError):
        parse_public_key(u'ssh-rsa not-base64!!')
    with pytest.raises(ValueError):
        parse_public_key(u'')
    with pytest.raises(ValueError):
        parse_public_key(u'ssh-unknown AAAAB3NzaC1yc2E=')


def test_special_usernames(cfg):
    assert check_new_user(u'new')
    assert not check_new_user(u'jojo')
    assert check_bye_user(u'logoff')
    assert not check_anonymous_user(u'anonymous')
    cfg.set('matrix', 'enable_anonymous', 'yes')
    assert check_anonymous_user(u'anonymous')
    cfg.set('nua', 'allow_apply', 'no')
    assert not check_new_user(u'new')
