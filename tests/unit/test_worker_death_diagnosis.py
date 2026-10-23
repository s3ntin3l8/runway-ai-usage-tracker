"""The xdist worker-death diagnosis must fire, and only for that failure.

When an xdist worker dies, the master cannot reschedule its nodes and xdist's
own ``KeyError`` takes the master down with an ``INTERNALERROR``. The result
is a CI failure that names neither the worker nor the cause, and it looks
exactly like a test failure at the hook level.

``tests/conftest.py`` installs a wrapper that turns that into a sentence a
reader can act on. These tests cover the wrapper's judgement.
"""

from __future__ import annotations

import inspect

from tests.fixtures.worker_death import (
    _is_xdist_frame,
    worker_death_diagnosis,
)

_XDIST_FRAME = "/site-packages/xdist/scheduler/loadscope.py"


def test_a_key_error_from_xdist_is_diagnosed():
    """The scheduler's KeyError is the failure this exists for."""
    diagnosis = worker_death_diagnosis(KeyError("node"), [_XDIST_FRAME])
    assert diagnosis is not None


def test_a_key_error_from_elsewhere_is_not_diagnosed():
    """A KeyError from the suite's own code is a different failure.

    It may be a real bug, and it deserves the traceback it already gets --
    not a note about a dead worker.
    """
    assert worker_death_diagnosis(KeyError("node"), ["app/core/config.py"]) is None


def test_another_exception_is_not_diagnosed():
    """Only the scheduler's KeyError is this failure."""
    diagnosis = worker_death_diagnosis(RuntimeError("boom"), ["/site-packages/xdist/dsession.py"])
    assert diagnosis is None


def test_the_diagnosis_names_the_cause():
    """It must tell the reader what to check, not just that something broke."""
    diagnosis = worker_death_diagnosis(KeyError("node"), [_XDIST_FRAME])
    assert diagnosis is not None
    assert "worker" in diagnosis
    assert "killed" in diagnosis
    assert "memory" in diagnosis


def test_a_windows_path_is_recognised():
    """Traceback paths on Windows use backslashes."""
    assert _is_xdist_frame(r"C:\venv\Lib\site-packages\xdist\scheduler\loadscope.py")
    assert not _is_xdist_frame(r"C:\venv\Lib\site-packages\pytest\main.py")


def test_the_hook_is_wired():
    """Deleting the conftest wiring must not silently disable the diagnosis."""
    from tests import conftest

    assert inspect.isgeneratorfunction(conftest.pytest_runtestloop)
