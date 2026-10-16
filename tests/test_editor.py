""" Tests for the full-screen editor script of the default board. """
# std imports
import importlib.util
import os
import threading
import time

# 3rd-party
import pytest

import x84

EDITOR_PATH = os.path.join(os.path.dirname(x84.__file__), 'default', 'editor.py')


@pytest.fixture
def editor(session):
    """ Return the editor script module, loaded within a session. """
    spec = importlib.util.spec_from_file_location('editor', EDITOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_editor(session, editor, keys, **kwargs):
    """
    Run the editor, sending ``keys`` as separate input events.

    A number among keys is a pause, in seconds, such as after the escape
    key, which is otherwise combined with the next key.  Output is read
    concurrently, so that the session never blocks on a full pipe.
    Returns the editor result and its output.
    """
    output = []
    done = threading.Event()

    def send():
        for key in keys:
            if isinstance(key, float):
                time.sleep(key)
            else:
                session.keys(key)

    def drain():
        while not done.is_set() or session.master_read.poll():
            if session.master_read.poll(0.05):
                event, data = session.master_read.recv()
                if event == 'output':
                    output.append(data[0])

    threads = [threading.Thread(target=send), threading.Thread(target=drain)]
    for thread in threads:
        thread.start()
    try:
        result = editor.main(**kwargs)
    finally:
        done.set()
        for thread in threads:
            thread.join()
    return result, u''.join(output)


#: pause longer than the escape key delay of blessed.
PAUSE = 0.5

SAVE = u'\x1a'
ABORT = u'\x01'


def test_type_and_save(session, editor):
    result, _ = run_editor(session, editor, [u'hello', u'\r', u'world', SAVE])
    assert result == u'hello\r\nworld'


def test_continue_draft_and_edit(session, editor):
    # move to end of the first line, delete a word, and save.
    keys = [u'\x1b[F', u'\x17', u'there', SAVE]
    result, _ = run_editor(session, editor, keys,
                           continue_draft=u'hello world\r\nsecond')
    assert result == u'hello there\r\nsecond'


def test_crlf_is_one_return(session, editor):
    result, _ = run_editor(session, editor, [u'a\r\nb', SAVE])
    assert result == u'a\r\nb'


def test_abort_unmodified_returns_false(session, editor):
    result, _ = run_editor(session, editor, [ABORT],
                           continue_draft=u'unchanged')
    assert result is False


def test_abort_modified_asks_confirmation(session, editor):
    # 'n' declines, typing continues, then abort is confirmed by 'y'.
    result, output = run_editor(
        session, editor, [u'x', ABORT, u'n', u'y', ABORT, u'y'])
    assert u'Abort, discarding your text?' in output
    assert result is False


def test_escape_menu_save(session, editor):
    result, output = run_editor(session, editor,
                                [u'text', u'\x1b', PAUSE, u's'])
    assert u'Menu:' in output
    assert result == u'text'


def test_escape_menu_returns(session, editor):
    result, _ = run_editor(session, editor,
                           [u'a', u'\x1b', PAUSE, u'\x1b', PAUSE, u'b', SAVE])
    assert result == u'ab'


def test_escape_combined_with_key_chooses(session, editor):
    # escape followed quickly by 's' is received as alt-s.
    result, output = run_editor(session, editor, [u'text', u'\x1bs'])
    assert u'Menu:' not in output
    assert result == u'text'


def test_escape_menu_undo(session, editor):
    result, _ = run_editor(session, editor,
                           [u'one two', u'\x1b', PAUSE, u'u', SAVE])
    assert result == u'one'


def test_unknown_sequence_not_inserted(session, editor):
    result, _ = run_editor(session, editor, [u'a', u'\x1b[99~', u'b', SAVE])
    assert result == u'ab'


def test_cut_and_paste(session, editor):
    # cut first line, move down, paste it above the last line.
    keys = [u'\x19', u'\x1b[B', u'\x15', SAVE]
    result, _ = run_editor(session, editor, keys,
                           continue_draft=u'one\r\ntwo\r\nthree')
    assert result == u'two\r\none\r\nthree'


def test_quote_window_inserts_marked_lines(session, editor):
    # mark the first and third lines of the quote, insert, then type.
    keys = [u'\x11', u' ', u'\x1b[B', u' ', u'\r', u'reply', SAVE]
    result, output = run_editor(
        session, editor, keys, quote=u'first\r\nsecond\r\nthird',
        quote_header=u'biff wrote:')
    assert u'^Q Quote' in output
    assert result == u'biff wrote:\r\n> first\r\n> third\r\nreply'


def test_quote_window_escape_cancels(session, editor):
    keys = [u'\x11', u' ', u'\x1b', PAUSE, u'x', SAVE]
    result, _ = run_editor(session, editor, keys, quote=u'first')
    assert result == u'x'


def test_quote_key_without_quote_is_ignored(session, editor):
    result, _ = run_editor(session, editor, [u'\x11', u'x', SAVE])
    assert result == u'x'


def test_title_displayed(session, editor):
    _, output = run_editor(session, editor, [SAVE], title=u'Re: hello')
    assert u'Re: hello' in output


def test_typing_redraws_only_changes(session, editor):
    # after 'abcdef' is displayed, typing 'g' writes only that, and
    # the column number of the status bar.
    _, output = run_editor(session, editor,
                           [u'abcdef', PAUSE, u'g', PAUSE, SAVE])
    assert output.count(u'abcdef') == 1


def test_resize_rewraps(session, editor):
    text = u' '.join([u'word'] * 30)
    session.send('refresh', ('resize', (40, 20)))
    result, output = run_editor(session, editor, [SAVE],
                                continue_draft=text)
    assert result == text


def test_draws_only_ascii(session, editor):
    # so that the editor displays the same for cp437 and utf8 terminals:
    # all but the text typed, and the help screen, is ascii.
    keys = [u'caf\xe9 中', PAUSE, u'\x11', PAUSE, u'\x1b', PAUSE,
            u'\x1b', PAUSE, SAVE]
    _, output = run_editor(session, editor, keys, quote=u'na\xefve',
                           title=u'Subject')
    for typed in (u'caf\xe9 中', u'na\xefve'):
        output = output.replace(typed, u'')
    assert all(ord(ucs) < 128 for ucs in output), [
        ucs for ucs in output if ord(ucs) >= 128]


def test_control_key_at_menu(session, editor):
    result, _ = run_editor(session, editor, [u'a', u'\x1b', PAUSE, SAVE])
    assert result == u'a'
