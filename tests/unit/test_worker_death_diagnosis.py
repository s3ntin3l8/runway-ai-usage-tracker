"""The xdist worker-death diagnosis must fire, and only for that failure.

When an xdist worker dies, the master cannot reschedule its nodes and xdist's
own ``KeyError`` takes the master down with an ``INTERNALERROR``. The result
is a CI failure that names neither the worker nor the cause, and it looks
exactly like a test failure at the hook level.

``tests/conftest.py`` installs a wrapper that turns that into a sentence a
reader can act on. These tests cover the wrapper's judgement.
"""

from __future__ import annotations

import pytest

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


def test_the_hook_names_the_failure_and_reraises(capsys):
    """Drive the hook with the failure it exists for.

    The ``KeyError`` has to come from inside xdist for the hook to recognise
    it, so this raises it the way the scheduler does -- with a stub whose
    worker collection is missing, exactly as it is when a worker dies -- and
    then throws that exception into the wrapper.

    This is the test that matters: replacing the hook's body with a bare
    ``return (yield)`` would leave every other test in this file green, and
    the diagnosis would silently stop happening.
    """
    from collections import OrderedDict

    from xdist.scheduler.loadscope import LoadScopeScheduling

    from tests import conftest

    class _SchedulerWhoseWorkerDied:
        def __init__(self) -> None:
            self.workqueue = OrderedDict([("scope", {})])
            self.assigned_work: dict = {}
            self.registered_collections: dict = {}

    with pytest.raises(KeyError) as raised:
        LoadScopeScheduling._assign_work_unit(_SchedulerWhoseWorkerDied(), "node")
    dead_worker = raised.value

    generator = conftest.pytest_runtestloop(None)
    next(generator)  # advance the wrapper to its yield
    with pytest.raises(KeyError):
        generator.throw(dead_worker)

    assert "An xdist worker died" in capsys.readouterr().err
