"""The xdist worker-death diagnosis must fire, and only for that failure.

When an xdist worker dies, the master cannot reschedule its nodes and xdist's
own ``KeyError`` takes the master down with an ``INTERNALERROR``. The result
is a CI failure that names neither the worker nor the cause, and it looks
exactly like a test failure at the hook level.

``tests/conftest.py`` reports it two ways: a ``pytest_testnodedown`` hook that
fires the moment the worker dies and stops the master cleanly, and a
run-loop wrapper that catches the crash if it happens anyway. These tests
cover both.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.fixtures.worker_death import (
    DEATH_SUMMARY,
    _is_xdist_frame,
    worker_death_diagnosis,
    worker_death_report,
    worker_id,
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


class _RecordingSession:
    """A stand-in for xdist's master session that records every write.

    A plain stub with a default attribute would keep a test green even if the
    hook stopped the master some other way, so the writes are recorded.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "writes", [])
        object.__setattr__(self, "shouldstop", False)

    def __setattr__(self, name: str, value: object) -> None:
        self.writes.append(name)
        object.__setattr__(self, name, value)


class _StubNode:
    """A stand-in for xdist's WorkerController."""

    def __init__(self, name: str = "gw3", workeroutput: dict | None = None) -> None:
        self.gateway = SimpleNamespace(id=name)
        self.workeroutput = workeroutput
        self.dsession = _RecordingSession()
        self.plugin_lookups: list[str] = []

        def get_plugin(plugin: str) -> object:
            self.plugin_lookups.append(plugin)
            return self.dsession if plugin == "dsession" else None

        self.config = SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=get_plugin))


def test_a_worker_that_finished_normally_is_not_reported(capsys):
    """xdist calls the hook for every worker that goes down.

    A worker that finished normally is not a death, so it must not be
    reported as one.
    """
    from tests import conftest

    node = _StubNode()
    conftest.pytest_testnodedown(node, None)

    assert capsys.readouterr().err == ""
    assert node.dsession.shouldstop is False


def test_a_dead_worker_is_reported_with_its_id_and_the_error(capsys):
    """The report names the worker and what the master saw."""
    from tests import conftest

    conftest.pytest_testnodedown(_StubNode(), "Not properly terminated")

    report = capsys.readouterr().err
    assert "gw3" in report
    assert "Not properly terminated" in report
    assert DEATH_SUMMARY.strip() in report


def test_a_worker_that_sent_no_output_is_reported_as_a_hard_kill(capsys):
    """xdist only has a worker's output if the worker sent it.

    A killed worker never reaches ``pytest_sessionfinish``, so its exit
    status and stderr never reach the master, and the report has to say so
    rather than imply they are missing.
    """
    from tests import conftest

    conftest.pytest_testnodedown(_StubNode(), "Not properly terminated")

    report = capsys.readouterr().err
    assert "sent no output" in report
    assert "pytest_sessionfinish" in report


def test_a_worker_that_sent_its_output_is_reported_with_it(capsys):
    """When the output exists, the report shows it."""
    from tests import conftest

    node = _StubNode(workeroutput={"exitstatus": 1, "stderr": "Worker crashed hard"})
    conftest.pytest_testnodedown(node, "Not properly terminated")

    report = capsys.readouterr().err
    assert "  the worker's exit status: 1" in report
    assert "Worker crashed hard" in report
    assert "sent no output" not in report


def test_a_worker_that_sent_an_exit_status_but_no_stderr_is_not_a_hard_kill(
    capsys,
):
    """The hard-kill branch is about the output as a whole.

    A worker that sent its exit status but no stderr did send output, so the
    report must not claim the bytes never reached the master.
    """
    from tests import conftest

    node = _StubNode(workeroutput={"exitstatus": 1})
    conftest.pytest_testnodedown(node, "Not properly terminated")

    report = capsys.readouterr().err
    assert "  the worker's exit status: 1" in report
    assert "sent no output" not in report


def test_the_master_is_left_to_recover():
    """The hook must not stop the master.

    xdist replaces a crashed worker by default, so a run that can still
    finish must be allowed to finish. Stopping the master here would turn a
    recoverable death into a failed run: with one worker killed and
    ``--dist loadfile -n 2``, the baseline is ``1 failed, 9 passed`` and the
    rest of the suite runs; aborting loses the rest of it.

    The guard is on *any* interference with the master, not on one attribute:
    the session records every write, and the plugin lookup is recorded too, so
    a hook that stopped the master by raising, or through some other
    attribute, would still fail this test.
    """
    from tests import conftest

    node = _StubNode()
    conftest.pytest_testnodedown(node, "Not properly terminated")

    assert node.dsession.writes == []
    assert node.plugin_lookups == []


def test_the_report_names_the_worker():
    """The report says which worker died."""
    node = _StubNode(name="gw7")
    assert worker_id(node) == "gw7"
    assert "gw7" in worker_death_report(node, "boom")
