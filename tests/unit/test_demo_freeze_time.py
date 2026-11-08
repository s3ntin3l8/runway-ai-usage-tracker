"""The demo's fixed clock (DEMO_NOW): scripts/demo/freeze_time.py and its use in scripts/demo/instance.py."""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.demo import instance
from scripts.demo.freeze_time import parse_demo_now

REPO = Path(__file__).resolve().parents[2]
NOW = "2026-10-15T10:00:00Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (NOW, datetime(2026, 10, 15, 10, 0, tzinfo=UTC)),
        ("2026-10-15T10:00:00", datetime(2026, 10, 15, 10, 0, tzinfo=UTC)),
        ("2026-10-15 10:00:00+00:00", datetime(2026, 10, 15, 10, 0, tzinfo=UTC)),
        ("2026-10-15T12:00:00+02:00", datetime(2026, 10, 15, 10, 0, tzinfo=UTC)),
        ("20261015T100000", datetime(2026, 10, 15, 10, 0, tzinfo=UTC)),
    ],
)
def test_parse_demo_now_reads_an_iso_instant(value: str, expected: datetime) -> None:
    assert parse_demo_now(value) == expected


@pytest.mark.parametrize("value", ["2026-10-15", "20261015", "yesterday", "", "10:00"])
def test_parse_demo_now_refuses_what_is_not_an_instant(value: str) -> None:
    # A bare date would read as 00:00 UTC, a day off whatever browser clock the scenes pair it with.
    with pytest.raises(SystemExit):
        parse_demo_now(value)


def _run_frozen(tmp_path: Path, demo_now: str | None) -> str:
    script = tmp_path / "now.py"
    script.write_text("from datetime import UTC, datetime\nprint(datetime.now(UTC).isoformat())\n")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)}
    if demo_now is not None:
        env["DEMO_NOW"] = demo_now
    done = subprocess.run(
        [sys.executable, "-m", "scripts.demo.freeze_time", str(script)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_the_target_runs_with_the_clock_at_demo_now(tmp_path: Path) -> None:
    ran = datetime.fromisoformat(_run_frozen(tmp_path, NOW))
    # The clock keeps ticking from DEMO_NOW, so timeouts behave; it does not jump to today.
    assert 0 <= (ran - datetime(2026, 10, 15, 10, 0, tzinfo=UTC)).total_seconds() < 30


def test_without_demo_now_the_target_runs_on_the_real_clock(tmp_path: Path) -> None:
    ran = datetime.fromisoformat(_run_frozen(tmp_path, None))
    assert abs((datetime.now(UTC) - ran).total_seconds()) < 30


def test_python_runs_through_freeze_time_only_when_the_clock_is_fixed() -> None:
    plain = instance._python(["-m", "uvicorn", "app.main:app"], frozen=False)
    assert plain[1:] == ["-m", "uvicorn", "app.main:app"]
    frozen = instance._python(["-m", "uvicorn", "app.main:app"], frozen=True)
    assert frozen[1:] == ["-m", "scripts.demo.freeze_time", "uvicorn", "app.main:app"]
    script = instance._python(["scripts/x.py"], frozen=True)
    assert script[1:] == ["-m", "scripts.demo.freeze_time", "scripts/x.py"]


def test_an_invalid_demo_now_stops_the_demo_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_NOW", "2026-10-15")

    def boom(*args, **kwargs):
        raise AssertionError("the demo started with an invalid DEMO_NOW")

    monkeypatch.setattr(instance, "assert_ports_free", boom)
    monkeypatch.setattr(instance, "ensure_spa_built", boom)
    with pytest.raises(SystemExit):
        instance.start_demo()
