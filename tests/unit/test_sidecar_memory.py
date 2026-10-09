"""Memory optimizations preserve delivery, attribution, and failure recovery."""

import io
import json
import sqlite3
import weakref
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.models.schemas import UsageEventPush
from scripts import sidecar
from scripts.sidecar_pkg.event_extractors import antigravity, hermes, opencode
from scripts.sidecar_pkg.sqlite_cursor import CursorReadError, iter_cursor
from sidecar_app import config, daemon, settings_server, updater
from tests.fixtures.hermes_fixture import make_hermes_db


def test_desktop_uses_one_sidecar_state(monkeypatch):
    assert config._sidecar is daemon._sidecar is sidecar
    monkeypatch.delenv("RUNWAY_UPDATE_CHANNEL", raising=False)
    monkeypatch.setattr(daemon._sidecar, "_UPDATE_CHANNEL", "beta")
    assert updater._resolve_channel() == "beta"
    assert daemon.DaemonRunner is sidecar.DaemonRunner


@pytest.mark.parametrize(
    "provider", ["anthropic", "chatgpt", "gemini", "xai", "opencode", "antigravity", "hermes"]
)
def test_serialization_releases_models_and_preserves_order_and_aliases(monkeypatch, provider):
    refs = []
    original_dump = UsageEventPush.model_dump

    def dump(event, **kwargs):
        index = int(event.event_id)
        if index:
            assert refs[index - 1]() is None
        return original_dump(event, **kwargs)

    def extract(*args, **kwargs):
        events = [
            UsageEventPush(
                provider_id="minimax" if i == 1 else provider,
                account_id="tagged" if i == 1 else "local",
                event_id=str(i),
                ts="2026-10-09T00:00:00Z",
                account_source="tag" if i == 1 else None,
            )
            for i in range(3)
        ]
        refs.extend(weakref.ref(event) for event in events)
        return events

    for name in (
        "_make_account_extractor",
        "_make_account_extractor_opencode",
        "_make_account_extractor_antigravity",
        "_make_account_extractor_hermes",
    ):
        monkeypatch.setattr(sidecar, name, lambda *args, **kwargs: extract)
    monkeypatch.setattr(UsageEventPush, "model_dump", dump)
    monkeypatch.setattr(sidecar, "_EVENT_WATERMARK_ALIASES", {})
    output = []
    assert (
        sidecar._extract_events_for_provider(
            provider,
            ["local"],
            watermark=MagicMock(),
            bootstrap_days=90,
            out_events=output,
            account_source="local",
        )
        == 0
    )
    assert [event["event_id"] for event in output] == ["0", "1", "2"]
    assert [event["account_source"] for event in output] == ["local", "tag", "local"]
    assert sidecar._EVENT_WATERMARK_ALIASES == {("minimax", "1"): (provider, "local")}
    assert all(ref() is None for ref in refs)


@pytest.mark.parametrize("raises", [False, True])
def test_runner_releases_cycle_aliases_even_on_failure(monkeypatch, raises):
    monkeypatch.setattr(
        sidecar, "_EVENT_WATERMARK_ALIASES", {("minimax", "1"): ("hermes", "local")}
    )
    runner = sidecar.DaemonRunner({})
    inner = (
        MagicMock(side_effect=RuntimeError("failed")) if raises else MagicMock(return_value=True)
    )
    monkeypatch.setattr(runner, "_run_once_impl", inner)
    if raises:
        with pytest.raises(RuntimeError):
            runner.run_once()
    else:
        assert runner.run_once() is True
    assert not sidecar._EVENT_WATERMARK_ALIASES
    assert not runner._cycle_running


def test_log_tails_preserve_line_contents_and_missing_file_behavior(tmp_path, monkeypatch):
    log = tmp_path / "sidecar.log"
    log.write_bytes(b"old\n" * 500 + b"invalid-\xff\nlast  \n")
    monkeypatch.setattr(sidecar, "get_log_path", lambda: log)
    monkeypatch.setattr(config, "get_log_path", lambda: log)
    assert sidecar._tail_log(2) == ["invalid-\ufffd", "last"]
    handler = object.__new__(settings_server._Handler)
    handler._send_json = MagicMock()
    handler._serve_logs()
    lines = handler._send_json.call_args.args[0]["lines"]
    assert len(lines) == 200
    assert lines[-2:] == ["invalid-\ufffd", "last"]
    log.unlink()
    assert sidecar._tail_log(2) == []
    handler._serve_logs()
    assert handler._send_json.call_args.args[0]["lines"] == []


@pytest.fixture
def queue_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "ensure_dirs", lambda: None)
    path = tmp_path / "2026-10-09.jsonl"
    path.write_text(
        "".join(
            json.dumps({"payload": {"events": [{"event_id": str(i)}]}}) + "\n" for i in range(3)
        )
    )
    return path


def test_failed_atomic_replacement_keeps_original_queue(queue_file, monkeypatch):
    original = queue_file.read_bytes()
    replies = iter([(True, {}, 200), (False, {}, 503), (False, {}, 503)])
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **k: next(replies))
    monkeypatch.setattr(sidecar.os, "replace", MagicMock(side_effect=OSError("replace failed")))
    assert sidecar.queue_flush("http://localhost", "test") == 1
    assert queue_file.read_bytes() == original
    assert list(queue_file.parent.iterdir()) == [queue_file]


@pytest.mark.parametrize("acknowledged", [False, True])
def test_failed_retention_write_keeps_original_queue(queue_file, monkeypatch, acknowledged):
    original = queue_file.read_bytes()
    replay = sidecar._replay_queue_lines

    class FailingWriter:
        def write(self, _line):
            raise OSError("disk full")

    replies = iter([(True, {}, 200)] * int(acknowledged) + [(False, {}, 503)])
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **k: next(replies))
    monkeypatch.setattr(
        sidecar,
        "_replay_queue_lines",
        lambda source, retained, *args: replay(source, FailingWriter(), *args),
    )
    assert sidecar.queue_flush("http://localhost", "test") == int(acknowledged)
    assert queue_file.read_bytes() == original
    assert list(queue_file.parent.iterdir()) == [queue_file]


def test_concurrent_append_is_not_overwritten(queue_file, monkeypatch):
    original = queue_file.read_bytes()
    new_entry = b'{"payload":{"events":[{"event_id":"appended"}]}}\n'
    appended = False

    def post(*args, **kwargs):
        nonlocal appended
        if not appended:
            appended = True
            with queue_file.open("ab") as writer:
                writer.write(new_entry)
        return False, {}, 503

    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", post)
    sidecar.queue_flush("http://localhost", "test")
    assert queue_file.read_bytes() == original + new_entry
    assert list(queue_file.parent.iterdir()) == [queue_file]


def test_queue_iterator_keeps_interrupted_suffix_without_bulk_reads():
    source = io.StringIO('{"payload":{"events":[{"event_id":"1"}]}}\n')
    retained = io.StringIO()
    stop = MagicMock()
    stop.is_set.return_value = True
    assert sidecar._replay_queue_lines(
        source, retained, "http://localhost", "test", stop, None
    ) == (0, 1, True)
    assert json.loads(retained.getvalue())["payload"]["events"][0]["event_id"] == "1"


def test_cursor_distinguishes_database_read_errors():
    class BrokenCursor:
        def __iter__(self):
            yield (1,)
            raise sqlite3.OperationalError("read failed")

    rows = iter_cursor(BrokenCursor())
    assert next(rows) == (1,)
    with pytest.raises(CursorReadError) as exc:
        next(rows)
    assert isinstance(exc.value.__cause__, sqlite3.OperationalError)


@pytest.mark.parametrize("provider", [opencode, antigravity, hermes])
def test_partial_database_reads_discard_events_and_state(tmp_path, monkeypatch, provider):
    db = tmp_path / "state.db"
    state = tmp_path / "watermark.json"
    conn = sqlite3.connect(db)
    if provider is opencode:
        conn.execute("CREATE TABLE message(id, session_id, time_created, data)")
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                "1",
                "session",
                1780003500000,
                json.dumps({"role": "assistant", "tokens": {"input": 100}, "modelID": "gpt"}),
            ),
        )
    elif provider is antigravity:
        conn.execute("CREATE TABLE gen_metadata(idx, data)")
        conn.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"blob",))
        # Nested metadata sufficient to produce one real event.
        monkeypatch.setattr(
            provider,
            "_parse_proto_fields",
            lambda blob: (
                {1: [b"inner"]}
                if blob == b"blob"
                else {4: [b"usage"]}
                if blob == b"inner"
                else {2: [100], 3: [20]}
            ),
        )
    else:
        conn.close()
        conn = make_hermes_db(str(db))
    conn.commit()
    conn.close()
    cursor_iterator = provider.iter_cursor

    def fail_after_first(cursor):
        rows = cursor_iterator(cursor)
        yield next(rows)
        raise CursorReadError("partial read")

    monkeypatch.setattr(provider, "iter_cursor", fail_after_first)
    since = datetime(2020, 1, 1, tzinfo=UTC)
    if provider is opencode:
        events = provider.parse_opencode_events(db, "local", since)
    elif provider is antigravity:
        events = provider.parse_antigravity_events([db], "local", since)
    else:
        events = provider.parse_hermes_events([db], "local", since, state_file=state)
    assert events == []
    assert not state.exists()


@pytest.mark.parametrize("settings_page", [False, True])
def test_log_readers_use_bounded_iteration(monkeypatch, settings_page):
    class IterationOnly(io.StringIO):
        def readlines(self, *args):
            raise AssertionError("whole-file read")

    module = settings_server if settings_page else sidecar
    monkeypatch.setattr(module, "open", lambda *a, **k: IterationOnly("old\nlast\n"), raising=False)
    if settings_page:
        handler = object.__new__(settings_server._Handler)
        handler._send_json = MagicMock()
        handler._serve_logs()
        assert handler._send_json.call_args.args[0]["lines"] == ["old", "last"]
    else:
        assert sidecar._tail_log(1) == ["last"]


def test_failed_first_batch_does_not_prepare_remaining_batches(monkeypatch):
    class Events(list):
        def __getitem__(self, index):
            if isinstance(index, slice):
                assert index.start == 0
            return super().__getitem__(index)

    events = Events({"event_id": str(i)} for i in range(3000))
    runner = sidecar.DaemonRunner({"api_url": "http://localhost", "api_key": "test"})
    monkeypatch.setattr(
        sidecar, "run_collection", lambda *a, **k: sidecar.CollectionResult([], events, 0, [])
    )
    monkeypatch.setattr(sidecar, "queue_flush", lambda *a, **k: 0)
    monkeypatch.setattr(
        sidecar, "http_post_signed_with_retry", lambda *a, **k: (False, "offline", 0)
    )
    queued = []
    monkeypatch.setattr(sidecar, "queue_push", lambda payload, *a: queued.append(payload) or True)
    assert runner.run_once() is False
    assert runner.status == "warn"
    assert len(queued) == 1
    assert [event["event_id"] for event in queued[0]["events"]] == [str(i) for i in range(1000)]
