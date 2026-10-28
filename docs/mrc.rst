=================
Multi Relay Chat
=================

Multi Relay Chat (MRC) is a chat between BBSes: users on many boards,
running Mystic, Synchronet, and others, talk together in rooms.  Each bbs
keeps one connection to the MRC server, which carries the chat for all
of its users.

The x/84 engine keeps that connection, when section ``[mrc]`` is enabled,
reconnecting when it is lost.  Users chat by the default board's script
``mrcchat``, chosen by ``mrc`` on the main menu.

Configuring
===========

Add to ``default.ini``::

    [mrc]
    enabled = yes
    server = mrc.bottomlessabyss.net
    port = 5001
    ssl = yes
    ssl_verify = yes
    bbsname = |10Back to the |12Future
    room = lobby
    info_web = https://bbs.example.com
    info_telnet = bbs.example.com:23
    info_ssh = bbs.example.com:22
    info_sysop = your name
    info_desc = what the bbs is about

``bbsname`` is the bbs's name as other BBSes see it, and allows pipe
codes; it is the ``bbsname`` in section ``[system]`` by default.  The
``info_`` options are displayed by the server's ``/info``.  Port
``5000`` is the server's port without ssl (``ssl = no``).  ``room`` is the
room first joined; a user may choose their own, by ``/set defaultroom``.

Restart x/84; its log shows it connecting::

    INFO     mrc.py: mrc: connected to mrc.bottomlessabyss.net:5001

Chatting
========

The screen is the room's name and topic on the top row, its messages
below, and the input line on the last row.  ``/?`` lists the client's
commands, such as ``/join``, ``/t`` (a direct message), ``/me``,
``/twit`` and ``/set``; ``/help`` lists the server's, such as
``/motd``, ``/rooms``, ``/bbses``, ``/users`` and ``/chatters``.  Page Up
and Page Down scroll back, Left and Right change the color of what you
say, and Tab completes a nick.

Messages are colored by pipe codes, ``|01`` to ``|15`` for text and
``|16`` to ``|23`` for its background.  MRC is ascii only: a character
typed outside ascii, such as an accented letter, is sent as its plain
letter.
