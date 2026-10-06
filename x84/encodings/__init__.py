import codecs
import logging
import re

_cache = {}
_aliases = {}

logger = logging.getLogger(__name__)


def normalize_encoding(encoding):
    return re.sub(
        r'[^\w_-]', '', encoding.lower()
    ).replace('-', '_')


def search_function(encoding):
    """ Codec search function for encodings provided by x/84. """
    encoding = normalize_encoding(encoding)
    encoding = _aliases.get(encoding, encoding)
    try:
        return _cache[encoding]
    except KeyError:
        pass

    if encoding not in CODECS:
        return None

    mod = __import__('x84.encodings.' + encoding, fromlist=['*'], level=0)
    _cache[encoding] = mod.getregentry()
    return _cache[encoding]


#: Codecs provided by x/84.
CODECS = ('amiga', 'atarist', 'cp437_art', 'cp437')

for _codec in CODECS:
    _mod = __import__('x84.encodings.' + _codec, fromlist=['*'], level=0)
    for _alias in getattr(_mod, 'getaliases', lambda: ())():
        _aliases.setdefault(normalize_encoding(_alias), _codec)

codecs.register(search_function)
