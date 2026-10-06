=====================
Dial-in modem callers
=====================

x/84 can answer the telephone.  A softmodem -- datamodem_, which carries
modem audio over a SIP telephone line -- answers each call, trains with the
caller's modem, and runs ``x84-modem-bridge`` to connect the call to x/84.
Callers dial a phone number with a modem and a terminal program, and reach
the board's login, just as in 1992.

::

    caller's modem --- telephone network --- SIP trunk --- datamodem
                                                              |
                                                       x84-modem-bridge
                                                              |
                                                    x/84 modem server (local)

Setting it up
=============

1. Enable the modem server in ``~/.x84/default.ini``::

    [modem]
    enabled = yes

   It listens on ``127.0.0.1`` port 6510 by default.  Only the bridge
   should connect to it; do not listen on a public address.

2. Run datamodem to answer calls, with the bridge as the program for each
   call::

    datamodem answer --server sip.example.com --username 1001 \
        --calls 0 --modulation v32bis --v42 detect --v42bis \
        --exec 'x84-modem-bridge --port 6510'

   ``--calls 0`` keeps answering, one call at a time.  For more than one
   line, run one datamodem per SIP account, each with its own
   ``--local-port`` and RTP port range.  See datamodem's README for the
   requirements of the SIP trunk: G.711 only, nothing transcoding the audio.

   ``x84-modem-bridge`` is installed with x/84; give the full path to it if
   datamodem's ``PATH`` does not include x/84's virtual environment.

3. Optionally, set a shared secret, so that only the bridge may connect::

    [modem]
    enabled = yes
    secret = something long and random

   and give it to the bridge in its environment, ``X84_MODEM_SECRET``, or
   by its ``--secret`` option.

Which modulation?
-----------------

The answering modem speaks one modulation, chosen by datamodem's
``--modulation``, and the caller's modem must speak it too.  ``v32bis``
(14400 bps) is answered by any V.32bis, V.34 or V.90 modem -- most that
people still own -- but not by a 2400 bps modem; ``v22bis`` is answered by
nearly everything, at 2400 bps.  ``v21`` survives the poorest telephone
path, at 300 bps.

Options
=======

All of section ``[modem]``:

- ``enabled``: whether to listen for the bridge, ``no`` by default.
- ``addr``, ``port``: where to listen, ``127.0.0.1`` and ``6510``.
- ``secret``: when set, the bridge must send the same value.
- ``term``: terminal type of modem callers, ``ansi``.  A modem tells the
  far end nothing about its terminal, which is assumed to be 80 x 24.
- ``encoding``: character encoding of modem callers, ``cp437``.
- ``art_min_rate``: artwork is not displayed to callers connected at a
  lower bit rate than this, ``2400`` by default; ``0`` always displays it.
  A screen of ANSI art is several kilobytes, and takes a minute or more to
  draw at 1200 bps.

Modem callers begin at script ``matrix_modem`` (option ``script_modem`` of
section ``[matrix]``), which tells the caller the rate they connected at,
then continues to the ordinary login.

In scripts
==========

The session of a modem caller has ``session.kind`` of ``'modem'``, and its
environment, ``session.env``, describes the call:

- ``CONNECT``: the modem's result code, such as ``14400 V.42/V.42bis``.
- ``MODEM_RATE``: the bit rate, such as ``14400``.
- ``MODEM_MODULATION``, ``MODEM_PROTOCOL``: such as ``v32bis`` and ``V.42``.
- ``CALLER_ID``, ``CALLER_NAME``, ``CALLED_ID``: as the telephone network
  provided them.  These may be empty, and are only as trustworthy as the
  SIP trunk that supplied them.

The caller ID is part of the session's id, such as
``modem-+15551234:57412``, as shown by Who's Online and the logs.

:func:`x84.bbs.output.slow_modem_rate` returns the rate of a caller too
slow for artwork, or 0; :func:`~x84.bbs.output.showart` skips artwork for
them unless given ``force=True``.

Output to a modem caller is paced to their line: the engine stops reading a
session's output while more than 2 KB of it waits to be sent, and the
session waits.  The caller's keystrokes are never queued behind minutes of
earlier output.

.. _datamodem: https://github.com/billsimon/datamodem
