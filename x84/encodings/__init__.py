import codecs
import functools
import logging
import re
import unicodedata

import wcwidth

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


def session_encoding(encoding, default='utf8'):
    """
    Return the name of ``encoding`` as used for sessions.

    That is, ``'utf8'`` for any name of utf-8, ``'cp437'`` for any name of
    cp437, and the codec's name otherwise.  An unknown ``encoding`` is
    logged, and ``default`` is returned.
    """
    try:
        name = codecs.lookup(encoding).name
    except (LookupError, TypeError):
        logger.warning('unknown encoding {0!r}, using {1!r}.'
                       .format(encoding, default))
        return default
    return 'utf8' if name == 'utf-8' else name


@functools.lru_cache(maxsize=None)
def output_codec(encoding):
    """
    Return codec and error handler used for output to a terminal of ``encoding``.

    Text decoded as cp437 art draws the glyphs of the low bytes: a cp437
    terminal is sent these as ``cp437_art``, which encodes each glyph as
    the byte it decodes from, so that it displays as it does for a utf-8
    terminal.
    """
    codec = encoding
    if codecs.lookup(encoding).name == 'cp437':
        codec = 'cp437_art'
    # the errors of charmap codecs do not name their encoding: an error
    # handler is registered for each codec.
    errors = 'x84_bestfit_' + normalize_encoding(codec)
    codecs.register_error(errors, functools.partial(_bestfit, codec))
    return codec, errors


def encode_output(ucs, encoding):
    """
    Encode unicode string ``ucs`` for a terminal of ``encoding``.

    Characters the encoding cannot represent are replaced by those that
    look alike, or by ``?``, filling the same number of cells that a
    utf-8 terminal would, so that the screen is laid out as it is there.
    """
    codec, errors = output_codec(encoding)
    try:
        return ucs.encode(codec)
    except UnicodeEncodeError:
        # combine letters and accents, where the encoding has the letter.
        return unicodedata.normalize('NFC', ucs).encode(codec, errors)


#: Replacements, by :func:`encode_output`, of characters that an encoding
#: cannot represent, but that look alike.
BESTFIT = {
    u'‘': u"'",       # LEFT SINGLE QUOTATION MARK
    u'’': u"'",       # RIGHT SINGLE QUOTATION MARK
    u'‚': u"'",       # SINGLE LOW-9 QUOTATION MARK
    u'‛': u"'",       # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    u'′': u"'",       # PRIME
    u'“': u'"',       # LEFT DOUBLE QUOTATION MARK
    u'”': u'"',       # RIGHT DOUBLE QUOTATION MARK
    u'„': u'"',       # DOUBLE LOW-9 QUOTATION MARK
    u'‟': u'"',       # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    u'″': u'"',       # DOUBLE PRIME
    u'‹': u'<',       # SINGLE LEFT-POINTING ANGLE QUOTATION MARK
    u'›': u'>',       # SINGLE RIGHT-POINTING ANGLE QUOTATION MARK
    u'‐': u'-',       # HYPHEN
    u'‑': u'-',       # NON-BREAKING HYPHEN
    u'‒': u'-',       # FIGURE DASH
    u'–': u'-',       # EN DASH
    u'—': u'-',       # EM DASH
    u'―': u'-',       # HORIZONTAL BAR
    u'−': u'-',       # MINUS SIGN
    u'•': u'∙',  # BULLET -> BULLET OPERATOR
    u'←': u'<',       # LEFTWARDS ARROW (its byte is ESC)
    u'▶': u'►',  # BLACK RIGHT-POINTING TRIANGLE
    u'◀': u'◄',  # BLACK LEFT-POINTING TRIANGLE
    u'♪': u'♬',  # EIGHTH NOTE (its byte is CR)
    u'♫': u'♬',  # BEAMED EIGHTH NOTES
    u'✓': u'√',  # CHECK MARK -> SQUARE ROOT
    u'✔': u'√',  # HEAVY CHECK MARK
    u'✗': u'x',       # BALLOT X
    u'✘': u'x',       # HEAVY BALLOT X
    u'━': u'─',  # BOX DRAWINGS HEAVY HORIZONTAL
    u'┃': u'│',  # BOX DRAWINGS HEAVY VERTICAL
    u'┏': u'┌',  # BOX DRAWINGS HEAVY DOWN AND RIGHT
    u'┓': u'┐',  # BOX DRAWINGS HEAVY DOWN AND LEFT
    u'┗': u'└',  # BOX DRAWINGS HEAVY UP AND RIGHT
    u'┛': u'┘',  # BOX DRAWINGS HEAVY UP AND LEFT
    u'┣': u'├',  # BOX DRAWINGS HEAVY VERTICAL AND RIGHT
    u'┫': u'┤',  # BOX DRAWINGS HEAVY VERTICAL AND LEFT
    u'┳': u'┬',  # BOX DRAWINGS HEAVY DOWN AND HORIZONTAL
    u'┻': u'┴',  # BOX DRAWINGS HEAVY UP AND HORIZONTAL
    u'╋': u'┼',  # BOX DRAWINGS HEAVY VERTICAL AND HORIZONTAL
    u'╭': u'┌',  # BOX DRAWINGS LIGHT ARC DOWN AND RIGHT
    u'╮': u'┐',  # BOX DRAWINGS LIGHT ARC DOWN AND LEFT
    u'╯': u'┘',  # BOX DRAWINGS LIGHT ARC UP AND LEFT
    u'╰': u'└',  # BOX DRAWINGS LIGHT ARC UP AND RIGHT
}


@functools.lru_cache(maxsize=4096)
def _bestfit_char(ucs, encoding):
    """ Return replacement of ``ucs``, which ``encoding`` cannot encode. """
    width = wcwidth.wcwidth(ucs)
    if width < 0:
        # control characters, which a terminal does not draw.
        return u''
    # a look-alike, or the letter of an accented letter.
    letters = u''.join(_ucs for _ucs in unicodedata.normalize('NFKD', ucs)
                       if not unicodedata.combining(_ucs))
    for candidate in (BESTFIT.get(ucs), letters):
        if not candidate or not 0 < wcwidth.wcswidth(candidate) <= width:
            continue
        try:
            candidate.encode(encoding)
        except UnicodeEncodeError:
            continue
        return candidate.ljust(width + len(candidate)
                               - wcwidth.wcswidth(candidate))
    return u'?' * width


def _bestfit(encoding, err):
    """ Error handler of :func:`encode_output`, for codec ``encoding``. """
    if not isinstance(err, UnicodeEncodeError):
        raise err
    return (u''.join(_bestfit_char(ucs, encoding)
                     for ucs in err.object[err.start:err.end]),
            err.end)
