"""Recognise the xdist crash that follows a killed worker.

When a worker dies, the master cannot reschedule the nodes that worker was
carrying: the collection it would have registered is missing, so xdist's
scheduler raises ``KeyError`` from its own internals and the master dies with
an ``INTERNALERROR``. pytest exits 3, and the only thing in the log is an xdist
traceback that names neither the worker nor the cause.

This module turns that into a sentence a reader can act on. The judgement
lives in :func:`worker_death_diagnosis`; the hook that applies it is in
``tests/conftest.py``.
"""

from __future__ import annotations

from collections.abc import Iterable

# xdist's own files live under .../site-packages/xdist/... on every platform.
_XDIST_PATH_MARKER = "/xdist/"

DIAGNOSIS = """\
An xdist worker died and the master could not recover it.

The scheduler tried to reschedule a test node whose worker was gone, so it
raised KeyError on the collection that worker never registered. pytest exits 3
with an INTERNALERROR; the traceback above is xdist's, not the suite's.

What usually kills a worker:
  * the OS, under memory pressure -- the worker is killed and its pipe closes
    without a clean exit (the "node down: Not properly terminated" line above
    names it);
  * pytest-timeout's thread method on Windows, which kills the whole worker
    process when a test overruns -- so a starved machine turns a slow test
    into a dead worker.

Check free memory and CPU before suspecting the suite: the same tree passes
on an idle machine.
"""


def _is_xdist_frame(filename: str) -> bool:
    """True when a traceback frame lives inside the xdist package."""
    normalised = filename.replace("\\", "/")
    return _XDIST_PATH_MARKER in normalised or normalised.startswith("xdist/")


def worker_death_diagnosis(exc: BaseException, filenames: Iterable[str]) -> str | None:
    """The diagnosis for xdist's worker-death crash, or None for anything else.

    The crash is a ``KeyError`` raised by the scheduler when it tries to
    reschedule a node whose worker died: the collection that worker would
    have registered is missing, so ``self.registered_collections[node]``
    raises. Anything else that raises out of the run loop is not this.

    The filenames are passed in rather than read off the exception so that a
    test can supply a synthetic list.
    """
    if not isinstance(exc, KeyError):
        return None
    if not any(_is_xdist_frame(name) for name in filenames):
        return None
    return DIAGNOSIS
