==========
Developers
==========

The x/84 telnet system is written in the Python_ programming language. With
prior programming experience you should be able to pick up the language quickly
by looking at the provided sample mods in the ``x84/default`` folder. If you
are completely new to Python_, it's recommended to read more about the
language, like provided by the free `Dive Into Python`_ 3 book by Mark Pilgrim.

Requirements
============

The following is step-by-step instructions for creating a developer environment
for making your own customizations of x/84's engine and api and building your
own ``'scriptpath'`` (defined by ``~/.x84/default.ini``).  You may also simply
install x/84 using pip.

x/84 requires Python_ 3.11 or later, on Linux, BSD, or macOS.  All of its
dependencies are available as binary wheels for common platforms, so a C
compiler is not usually necessary.

Virtual environment
-------------------

A virtual environment ensures you can install x/84 and its dependencies
without root access, without affecting system libraries or other python
projects::

      python3 -m venv ~/x84-env
      . ~/x84-env/bin/activate

Install editable version
------------------------

Instead of installing x84 as a complete package, we use ``pip`` to install
an *editable* version from a git clone -- this is so that when a modification
is done to the files in our local project directory, they are immediately
reflected in the ``x84`` server anytime the virtual environment is activated::

   git clone https://github.com/jquast/x84.git
   cd x84
   pip install --editable '.[test]'

Running tests
-------------

The test suite includes unit tests and integration tests, which start a real
server on free ports and drive it by telnet and ssh::

   pytest

Starting x/84
-------------

::

      x84

Scripts of the ``scriptpath`` folder are re-loaded each time they are
called, so changes to the default board are seen on your next visit to that
script, without restarting the server.  Changes to the ``x84`` package itself,
and to the configuration files, require a restart.


As a service
------------

To listen on privileged ports (such as telnet on port 23) without running as
root, run x/84 as a dedicated user with the ``CAP_NET_BIND_SERVICE``
capability.  For example, a systemd unit, ``/etc/systemd/system/x84.service``::

    [Unit]
    Description=x/84 BBS
    After=network-online.target
    Wants=network-online.target

    [Service]
    User=x84
    ExecStart=/opt/x84/bin/x84 --config=/etc/x84/default.ini --logger=/etc/x84/logging.ini
    AmbientCapabilities=CAP_NET_BIND_SERVICE
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target

x/84 shuts down gracefully, disconnecting all sessions, when it receives
``SIGTERM``.


x84 Usage
---------

Optional command line arguments,

    ``--config=`` alternate bbs configuration filepath

    ``--logger=`` alternate logging configuration filepath

    ``--version`` display version and exit

By default these are, in order of preference: ``/etc/x84/default.ini``
and ``/etc/x84/logging.ini``, or ``~/.x84/default.ini`` and
``~/.x84/logging.ini``.


Contributing using git
======================

If you intend to contribute patches or new mods to the x/84 telnet system, you
should `fork the repository <https://help.github.com/articles/fork-a-repo>`_
and clone over ssh.

Features should be developed into a branch, pushed to github, and when satisfied
with your changes and you wish to have them included in the base distribution,
you should
`create a pull request <https://help.github.com/articles/creating-a-pull-request>`_.

.. _git: http://git-scm.org/
.. _Python: https://www.python.org/
.. _Dive Into Python: https://diveintopython3.net/
