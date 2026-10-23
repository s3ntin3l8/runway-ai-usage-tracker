"""Report a worker that died, and the crash that can follow it.

xdist replaces a crashed worker by default, so a single death does not end the
run: the crashed node's remaining work is rescheduled and the crashing test is
reported as a normal failure. When the master *cannot* replace the worker it
needs -- the collection that worker would have registered is missing -- the
scheduler raises ``KeyError`` from its own internals and the master dies with
an ``INTERNALERROR`` (pytest exit 3), leaving an xdist traceback that names
neither the worker nor the cause.

This module turns that into a sentence a reader can act on. The judgement
lives in :func:`worker_death_diagnosis` and :func:`worker_death_report`; the
hooks that apply them are in ``tests/conftest.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# xdist's own files live under .../site-packages/xdist/... on every platform.
_XDIST_PATH_MARKER = "/xdist/"

#: What usually kills a worker, and what to check. Shared by both reports:
#: the one that runs before the master dies, and the one that runs after.
DEATH_SUMMARY = """\
What usually kills a worker:
  * the OS, under memory pressure -- the worker is killed and its pipe closes
    without a clean exit (the "node down: Not properly terminated" line
    names it);
  * pytest-timeout's thread method on Windows, which kills the whole worker
    process when a test overruns -- so a starved machine turns a slow test
    into a dead worker.

Check free memory and CPU before suspecting the suite: the same tree passes
on an idle machine.
"""

#: The report the run-loop wrapper prints, once the master has already died.
DIAGNOSIS = (
    "An xdist worker died and the master could not recover it.\n"
    "\n"
    "The scheduler tried to reschedule a test node whose worker was gone, so\n"
    "it raised KeyError on the collection that worker never registered. pytest\n"
    "exits 3 with an INTERNALERROR; the traceback below is xdist's, not the\n"
    "suite's.\n"
    "\n" + DEATH_SUMMARY
)


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


def worker_id(node: Any) -> str:
    """The id of the worker xdist is talking about, e.g. ``gw3``."""
    return str(getattr(getattr(node, "gateway", None), "id", "?"))


def worker_death_report(node: Any, error: object) -> str:
    """What the master knows about a worker that died.

    ``node`` is xdist's ``WorkerController`` and ``error`` is what the master
    saw when it went down.

    The worker's exit status and output are only there when the worker
    managed to send them: xdist captures those in ``pytest_sessionfinish``,
    and a worker that is killed never gets there. So for a hard kill the
    report says so rather than pretending the bytes exist.
    """
    output = getattr(node, "workeroutput", None) or {}
    lines = [
        f"An xdist worker ({worker_id(node)}) died.",
        "",
        f"  what the master saw: {error}",
    ]
    if output:
        exitstatus = output.get("exitstatus")
        if exitstatus is not None:
            lines.append(f"  the worker's exit status: {exitstatus}")
        stderr = (output.get("stderr") or "").strip()
        if stderr:
            lines += ["", "  the worker's stderr:", stderr]
    else:
        lines += [
            "",
            "  The worker sent no output, so its exit status and stderr never",
            "  reached the master: xdist captures those in pytest_sessionfinish,",
            "  and a worker that is killed never gets there. That is a hard kill.",
        ]
    lines += [
        "",
        "The test that was running on that worker is reported as a failure.",
        "xdist reschedules the work that worker was carrying, so the run may",
        "still finish; the master only dies with an INTERNALERROR when it",
        "cannot replace the worker it needs.",
        "",
        DEATH_SUMMARY.rstrip(),
    ]
    return "\n".join(lines)
