"""
Text buffer of the full-screen editor for x/84.

A document is a list of paragraphs, each a line of text without line
breaks.  Paragraphs are soft-wrapped at word boundaries to the width of
the editor for display, and the cursor is a (paragraph, offset) position
into them, so that the text is unaffected by the width of the terminal.

This module has no knowledge of terminals; ``default/editor.py`` renders
a :class:`TextBuffer` and translates keystrokes to its methods.
"""
# std imports
import functools
import re

# 3rd party
import wcwidth

#: line separator of documents given and returned.
HARDWRAP = u'\r\n'

#: matches any line break of a document given, of any convention.
RE_LINEBREAK = re.compile(r'\r\n|\n|\r')

#: matches the prefix of a quoted line, such as ``'> '`` or ``'>> '``.
RE_QUOTED = re.compile(r'^\s*>')

#: maximum number of undo levels.
UNDO_LEVELS = 50


def char_width(ucs):
    """
    Return the number of terminal cells occupied by character ``ucs``.

    Control characters, which wcwidth reports as -1, are displayed as a
    placeholder of one cell.
    """
    width = wcwidth.wcwidth(ucs)
    return 1 if width < 0 else width


def text_width(text):
    """ Return the number of terminal cells occupied by ``text``. """
    return sum(char_width(ucs) for ucs in text)


@functools.lru_cache(maxsize=4096)
def wrap_offsets(text, width):
    """
    Return tuple of offsets into ``text`` at which each row begins.

    Text is wrapped at word boundaries to fit ``width`` cells.  Spaces
    that end a row remain on that row (and may extend past ``width``),
    and words longer than ``width`` are broken.  The first offset is
    always 0, so that an empty paragraph is a single empty row.
    """
    starts = [0]
    row_start, col, last_break = 0, 0, None
    idx = 0
    while idx < len(text):
        ucs = text[idx]
        cells = char_width(ucs)
        if ucs == u' ':
            col += cells
            idx += 1
            last_break = idx
            continue
        if col + cells > width and idx > row_start:
            if last_break is not None and last_break > row_start:
                row_start = last_break
            else:
                row_start = idx
            starts.append(row_start)
            col = text_width(text[row_start:idx])
            last_break = None
            # the carried word may itself overflow, re-evaluate.
            continue
        col += cells
        idx += 1
    return tuple(starts)


def split_paragraphs(text):
    """ Return list of paragraphs of a document, ``text``. """
    if not text:
        return [u'']
    return [para.expandtabs(8) for para in RE_LINEBREAK.split(text)]


def quote_lines(text, width):
    """
    Return list of lines quoting document ``text``, wrapped to ``width``.

    Lines are prefixed by ``'> '``, or by ``'>'`` when already quoted.
    """
    lines = []
    for para in split_paragraphs(text.rstrip()):
        para = para.rstrip()
        if RE_QUOTED.match(para):
            lines.append(u'>' + para.lstrip())
            continue
        starts = wrap_offsets(para, max(1, width - 2)) + (len(para),)
        for begin, end in zip(starts, starts[1:]):
            lines.append((u'> ' + para[begin:end]).rstrip())
    return lines


class TextBuffer(object):

    """
    An editable document of paragraphs with a cursor.

    :ivar list paragraphs: text of each paragraph.
    :ivar int para: paragraph index of cursor.
    :ivar int pos: offset of cursor into its paragraph.
    :ivar bool overwrite: whether typed characters replace existing ones.
    :ivar bool modified: whether the document has changed.
    """

    def __init__(self, text=u'', width=79, max_length=65536):
        self.paragraphs = split_paragraphs(text)
        self.width = width
        self.max_length = max_length
        self.para, self.pos = 0, 0
        self.overwrite = False
        self.modified = False
        self.clipboard = []
        self._goal_col = None
        self._undo = []
        self._undo_group = None

    # document

    @property
    def text(self):
        """ Document as a string of paragraphs separated by CR+LF. """
        return HARDWRAP.join(self.paragraphs)

    @property
    def length(self):
        """ Number of characters of document, counting line breaks as 1. """
        return sum(len(para) for para in self.paragraphs) + (
            len(self.paragraphs) - 1)

    @property
    def is_empty(self):
        """ Whether document contains nothing but whitespace. """
        return not any(para.strip() for para in self.paragraphs)

    # layout

    def _starts(self, para):
        return wrap_offsets(self.paragraphs[para], max(1, self.width))

    def rows(self):
        """
        Return list of displayed rows, wrapped to :attr:`width`.

        Each row is a tuple of ``(para, begin, end)``, the paragraph index
        and the slice of its text displayed on that row.
        """
        result = []
        for para, text in enumerate(self.paragraphs):
            starts = self._starts(para) + (len(text),)
            for begin, end in zip(starts, starts[1:]):
                result.append((para, begin, end))
        return result

    def row_text(self, row):
        """ Return text of ``row``, as returned by :meth:`rows`. """
        para, begin, end = row
        return self.paragraphs[para][begin:end]

    def _row_of(self, para, pos):
        """ Return index of the row within ``para`` displaying ``pos``. """
        starts = self._starts(para)
        idx = 0
        for idx in range(len(starts) - 1, -1, -1):
            if starts[idx] <= pos:
                break
        return idx

    def cursor_rowcol(self):
        """ Return the (row, column) cell of cursor of displayed rows. """
        row = sum(len(self._starts(para)) for para in range(self.para))
        idx = self._row_of(self.para, self.pos)
        begin = self._starts(self.para)[idx]
        col = text_width(self.paragraphs[self.para][begin:self.pos])
        return row + idx, col

    def _row_bounds(self, para, idx):
        """ Return cursor positions (first, last) within row ``idx`` of para. """
        starts = self._starts(para)
        first = starts[idx]
        if idx + 1 < len(starts):
            # the offset beginning the next row would be displayed there.
            return first, starts[idx + 1] - 1
        return first, len(self.paragraphs[para])

    def _locate(self, row, col):
        """ Move cursor to cell (row, col) of displayed rows, or nearest. """
        for para in range(len(self.paragraphs)):
            num = len(self._starts(para))
            if row < num:
                break
            row -= num
        else:
            para, row = len(self.paragraphs) - 1, len(self._starts(para)) - 1
        first, last = self._row_bounds(para, row)
        text = self.paragraphs[para]
        pos, cells = first, 0
        while pos < last:
            cells += char_width(text[pos])
            if cells > col:
                break
            pos += 1
        self.para, self.pos = para, pos

    def locate(self, row, col):
        """ Move cursor to cell (row, col) of displayed rows, or nearest. """
        self._locate(row, col)
        self._moved()

    def replace_rows(self, changes):
        """
        Replace the text of displayed rows, as typed over them at once.

        A 3270 terminal sends the rows that were changed, by attention key.
        Each row's text replaces its part of its paragraph, which is wrapped
        again.  A paragraph whose rows are all made empty is removed.  Rows
        past the end of the document are added as paragraphs: one row
        continues the paragraph of the row above it when that row, or the
        last of the document, was typed to its end, as a 3270 continues
        typing on the next row.

        :param dict changes: text by index of displayed row, as of
            :meth:`rows`.
        :returns: whether the document was changed; it is not when it would
            exceed :attr:`max_length`.
        """
        rows = self.rows()
        paragraphs = list(self.paragraphs)
        replaced = {}
        # last row first, so that the offsets of rows before it are kept.
        for idx in sorted((idx for idx in changes if idx < len(rows)),
                          reverse=True):
            para, begin, end = rows[idx]
            old, new = paragraphs[para][begin:end], changes[idx].rstrip(u' ')
            if idx + 1 < len(rows) and rows[idx + 1][0] == para and new:
                # the spaces that end a wrapped row separate it from the
                # next, though not displayed by a 3270.
                new += old[len(old.rstrip(u' ')):] or u''
            paragraphs[para] = paragraphs[para][:begin] + new + (
                paragraphs[para][end:])
            replaced.setdefault(para, set()).add(idx)
        for para in sorted(replaced, reverse=True):
            para_rows = set(idx for idx, row in enumerate(rows)
                            if row[0] == para)
            if (para_rows <= replaced[para] and not paragraphs[para].strip()
                    and len(paragraphs) > 1):
                del paragraphs[para]
        added, prior = [], len(rows) - 1
        full = text_width(changes.get(prior, u'')) >= self.width
        for idx in sorted(idx for idx in changes if idx >= len(rows)):
            text = changes[idx]
            if idx == prior + 1 and full:
                if added:
                    added[-1] += text
                else:
                    paragraphs[-1] += text
            else:
                added.extend([u''] * (idx - prior - 1))
                added.append(text)
            prior, full = idx, text_width(text) >= self.width
        added = [para.rstrip(u' ') for para in added]
        while added and not added[-1]:
            added.pop()
        paragraphs.extend(added)
        if paragraphs == self.paragraphs:
            return False
        if sum(len(para) for para in paragraphs) + len(
                paragraphs) - 1 > self.max_length:
            return False
        self._checkpoint()
        self.paragraphs = paragraphs
        self.para = min(self.para, len(paragraphs) - 1)
        self.pos = min(self.pos, len(paragraphs[self.para]))
        self._end_group()
        return True

    # undo

    def _checkpoint(self, group=None):
        """
        Record the document for undo, before a change.

        Consecutive changes of the same ``group`` share one record, such
        as the typing of a word.
        """
        self.modified = True
        self._goal_col = None
        if group is not None and group == self._undo_group:
            return
        self._undo_group = group
        self._undo.append((list(self.paragraphs), self.para, self.pos))
        del self._undo[:-UNDO_LEVELS]

    def undo(self):
        """ Revert the last change, return False when there is none. """
        if not self._undo:
            return False
        self.paragraphs, self.para, self.pos = self._undo.pop()
        self._undo_group = None
        self._goal_col = None
        return True

    def _end_group(self):
        self._undo_group = None

    # editing

    def insert(self, text):
        """ Insert ``text``, which contains no line breaks, at cursor. """
        if self.length + len(text) > self.max_length:
            return False
        line = self.paragraphs[self.para]
        # each word typed is a separate undo record
        group = 'insert' if not (
            text == u' ' and self.pos and line[self.pos - 1] != u' '
        ) else None
        self._checkpoint(group)
        replace = len(text) if self.overwrite else 0
        self.paragraphs[self.para] = (
            line[:self.pos] + text + line[self.pos + replace:])
        self.pos += len(text)
        if group is None:
            self._undo_group = 'insert'
        return True

    def newline(self):
        """ Split paragraph at cursor. """
        if self.length + 1 > self.max_length:
            return False
        self._checkpoint()
        line = self.paragraphs[self.para]
        self.paragraphs[self.para:self.para + 1] = [
            line[:self.pos], line[self.pos:]]
        self.para, self.pos = self.para + 1, 0
        self._end_group()
        return True

    def backspace(self):
        """ Delete character left of cursor, joining paragraphs at start. """
        if self.pos:
            self._checkpoint('delete')
            line = self.paragraphs[self.para]
            self.paragraphs[self.para] = line[:self.pos - 1] + line[self.pos:]
            self.pos -= 1
        elif self.para:
            self._checkpoint()
            prior = self.paragraphs[self.para - 1]
            self.paragraphs[self.para - 1] = prior + self.paragraphs[self.para]
            del self.paragraphs[self.para]
            self.para, self.pos = self.para - 1, len(prior)
            self._end_group()
        else:
            return False
        return True

    def delete(self):
        """ Delete character at cursor, joining paragraphs at end. """
        line = self.paragraphs[self.para]
        if self.pos < len(line):
            self._checkpoint('delete-forward')
            self.paragraphs[self.para] = line[:self.pos] + line[self.pos + 1:]
        elif self.para + 1 < len(self.paragraphs):
            self._checkpoint()
            self.paragraphs[self.para] = line + self.paragraphs[self.para + 1]
            del self.paragraphs[self.para + 1]
            self._end_group()
        else:
            return False
        return True

    def delete_word_left(self):
        """ Delete word and any whitespace left of cursor. """
        if not self.pos:
            return self.backspace()
        self._checkpoint()
        line = self.paragraphs[self.para]
        begin = len(line[:self.pos].rstrip())
        begin = line.rfind(u' ', 0, begin) + 1
        self.paragraphs[self.para] = line[:begin] + line[self.pos:]
        self.pos = begin
        self._end_group()
        return True

    def cut_paragraph(self):
        """
        Remove paragraph at cursor to clipboard.

        Paragraphs cut consecutively accumulate in the clipboard.
        """
        if self._undo_group != 'cut':
            self.clipboard = []
        self._checkpoint('cut')
        self.clipboard.append(self.paragraphs[self.para])
        if len(self.paragraphs) == 1:
            self.paragraphs = [u'']
        else:
            del self.paragraphs[self.para]
            self.para = min(self.para, len(self.paragraphs) - 1)
        self.pos = 0
        return True

    def paste(self):
        """ Insert paragraphs of clipboard before paragraph at cursor. """
        if not self.clipboard:
            return False
        if self.length + len(HARDWRAP.join(self.clipboard)) > self.max_length:
            return False
        self._checkpoint()
        self.paragraphs[self.para:self.para] = list(self.clipboard)
        self.para += len(self.clipboard)
        self.pos = 0
        self._end_group()
        return True

    def insert_paragraphs(self, lines):
        """
        Insert ``lines`` as paragraphs, such as a quotation.

        They are inserted at the cursor: replacing an empty paragraph, or
        before or after the paragraph at cursor when at its start or not.
        The cursor is moved to an empty paragraph following them.
        """
        if not lines:
            return False
        if self.length + len(HARDWRAP.join(lines)) > self.max_length:
            return False
        self._checkpoint()
        if not self.paragraphs[self.para]:
            del self.paragraphs[self.para]
        elif self.pos:
            self.para += 1
        self.paragraphs[self.para:self.para] = list(lines)
        self.para += len(lines)
        if self.para == len(self.paragraphs) or self.paragraphs[self.para]:
            self.paragraphs.insert(self.para, u'')
        self.pos = 0
        self._end_group()
        return True

    # movement

    def _moved(self):
        self._goal_col = None
        self._end_group()

    def left(self):
        """ Move cursor one character left. """
        if self.pos:
            self.pos -= 1
        elif self.para:
            self.para -= 1
            self.pos = len(self.paragraphs[self.para])
        self._moved()

    def right(self):
        """ Move cursor one character right. """
        if self.pos < len(self.paragraphs[self.para]):
            self.pos += 1
        elif self.para + 1 < len(self.paragraphs):
            self.para, self.pos = self.para + 1, 0
        self._moved()

    def word_left(self):
        """ Move cursor to beginning of word, or of the prior word. """
        if not self.pos:
            return self.left()
        line, pos = self.paragraphs[self.para], self.pos
        while pos and line[pos - 1] == u' ':
            pos -= 1
        while pos and line[pos - 1] != u' ':
            pos -= 1
        self.pos = pos
        self._moved()

    def word_right(self):
        """ Move cursor to beginning of next word. """
        line, pos = self.paragraphs[self.para], self.pos
        if pos == len(line):
            return self.right()
        while pos < len(line) and line[pos] != u' ':
            pos += 1
        while pos < len(line) and line[pos] == u' ':
            pos += 1
        self.pos = pos
        self._moved()

    def vertical(self, delta):
        """ Move cursor ``delta`` rows down (or up, when negative). """
        row, col = self.cursor_rowcol()
        goal = col if self._goal_col is None else self._goal_col
        total = len(self.rows())
        target = max(0, min(total - 1, row + delta))
        if target == row and delta < 0:
            self.para = self.pos = 0
            goal = 0
        elif target == row and delta > 0:
            self.para = len(self.paragraphs) - 1
            self.pos = len(self.paragraphs[self.para])
        else:
            self._locate(target, goal)
        self._end_group()
        self._goal_col = goal

    def up(self, num=1):
        """ Move cursor ``num`` rows up. """
        self.vertical(-num)

    def down(self, num=1):
        """ Move cursor ``num`` rows down. """
        self.vertical(num)

    def home(self):
        """ Move cursor to beginning of row. """
        self.pos = self._row_bounds(
            self.para, self._row_of(self.para, self.pos))[0]
        self._moved()

    def end(self):
        """ Move cursor to end of row. """
        self.pos = self._row_bounds(
            self.para, self._row_of(self.para, self.pos))[1]
        self._moved()

    def top(self):
        """ Move cursor to beginning of document. """
        self.para = self.pos = 0
        self._moved()

    def bottom(self):
        """ Move cursor to end of document. """
        self.para = len(self.paragraphs) - 1
        self.pos = len(self.paragraphs[self.para])
        self._moved()
