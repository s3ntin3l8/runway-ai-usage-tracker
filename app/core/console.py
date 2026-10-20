"""UTF-8 console streams for the CLI entry points.

Windows gives a *redirected* stdout/stderr the ANSI code page (cp1252 by
default) instead of UTF-8, so a command that prints a character outside it —
an arrow in a docstring, a warning sign in a log line — dies with
UnicodeEncodeError as soon as its output is piped, captured by a test, or
written to a log file. The repo's own tooling sidesteps this by running with
``PYTHONUTF8=1`` (see ``scripts/dev_checks.py``); these helpers give the
documented commands the same guarantee when a user runs them directly.

The fix has to happen in-process: ``PYTHONUTF8`` and ``-X utf8`` are both
read by the interpreter *before* any user code runs.
"""

from __future__ import annotations

import sys
from typing import TextIO


def force_utf8_console(streams: tuple[TextIO | None, ...] | None = None) -> None:
    """Reconfigure the console streams to UTF-8 so non-ASCII output cannot crash.

    A windowed build has ``sys.stdout is None``, and a closed or detached
    stream cannot be reconfigured at all — both are ignored rather than
    raised, because a logging call must never take the process down.
    """
    targets = (sys.stdout, sys.stderr) if streams is None else streams
    for stream in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
