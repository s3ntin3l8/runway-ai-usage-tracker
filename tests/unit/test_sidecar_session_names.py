"""Session-name plumbing in the sidecar: changed-only state, payload, hardening."""

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from scripts import sidecar
from scripts.sidecar_pkg import session_names


@pytest.fixture(autouse=True)
def _clean_state():
    session_names.clear_state()
    yield
    session_names.clear_state()


def _rec(session: str, name: str, kind: str = "agent", provider: str = "anthropic"):
    return session_names.make_record(provider, session, name, kind, "claude_code")


class TestSelectAndCommit:
    def test_a_new_name_is_sent_then_not_again(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        records = [_rec("s1", "placeholder-one")]
        sent = session_names.select_for_push(records)
        assert [r["name"] for r in sent] == ["placeholder-one"]
        session_names.commit(sent)
        assert session_names.select_for_push(records) == []

    def test_nothing_is_marked_sent_before_commit(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        records = [_rec("s1", "placeholder-one")]
        assert session_names.select_for_push(records)
        session_names.discard()
        assert session_names.select_for_push(records)

    def test_a_rename_sends_only_the_changed_kind(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        first = [_rec("s1", "name-a"), _rec("s1", "Title", "generated")]
        session_names.commit(session_names.select_for_push(first))
        second = [_rec("s1", "name-b"), _rec("s1", "Title", "generated")]
        sent = session_names.select_for_push(second)
        assert [(r["kind"], r["name"]) for r in sent] == [("agent", "name-b")]

    def test_the_last_record_per_key_wins(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        sent = session_names.select_for_push([_rec("s1", "old"), _rec("s1", "new")])
        assert [r["name"] for r in sent] == ["new"]

    def test_invalid_records_are_dropped(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        bad: list[Any] = [
            None,
            {"provider_id": "anthropic", "session_id": "s", "name": "x", "kind": "other"},
            _rec("s1", "   "),
            _rec("", "x"),
        ]
        assert session_names.select_for_push(bad) == []

    def test_the_request_is_capped_and_the_rest_follows(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        records = [_rec(f"s{i}", "name") for i in range(session_names.MAX_PER_PUSH + 5)]
        first = session_names.select_for_push(records)
        assert len(first) == session_names.MAX_PER_PUSH
        session_names.commit(first)
        rest = session_names.select_for_push(records)
        assert len(rest) == 5

    def test_an_overlong_name_is_cut(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        sent = session_names.select_for_push([_rec("s1", "x" * 5000)])
        assert len(sent[0]["name"]) == session_names.MAX_NAME_LENGTH


class TestFileSignatures:
    def test_a_scanned_file_is_skipped_only_after_commit(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        signature = session_names.file_signature(transcript)
        assert signature
        session_names.stage_files({transcript: signature})
        assert not session_names.file_unchanged(transcript)
        session_names.commit([])
        assert session_names.file_unchanged(transcript)
        transcript.write_text("xy")
        assert not session_names.file_unchanged(transcript)

    def test_a_discarded_scan_does_not_hide_the_file(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        session_names.stage_files({transcript: session_names.file_signature(transcript) or ""})
        session_names.discard()
        session_names.commit([])
        assert not session_names.file_unchanged(transcript)

    def test_a_missing_file_has_no_signature(self, tmp_path):
        assert session_names.file_signature(tmp_path / "gone") is None
        assert not session_names.file_unchanged(tmp_path / "gone")

    def test_a_capped_cycle_does_not_commit_file_signatures(self, tmp_path):
        session_names.configure(tmp_path / "state.json")
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        session_names.stage_files({transcript: session_names.file_signature(transcript) or ""})
        records = [_rec(f"s{i}", "n") for i in range(session_names.MAX_PER_PUSH + 1)]
        session_names.commit(session_names.select_for_push(records))
        assert not session_names.file_unchanged(transcript)


class TestStateFile:
    def _commit_one(self, path: Path) -> None:
        session_names.configure(path)
        session_names.commit(session_names.select_for_push([_rec("secret-session", "secret-name")]))

    def test_state_survives_a_restart_and_holds_no_text(self, tmp_path):
        path = tmp_path / "state.json"
        self._commit_one(path)
        text = path.read_text()
        assert "secret-name" not in text and "secret-session" not in text
        session_names.clear_state()
        session_names.configure(path)
        assert session_names.select_for_push([_rec("secret-session", "secret-name")]) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
    def test_the_file_is_private_and_no_temporary_is_left(self, tmp_path):
        path = tmp_path / "state.json"
        self._commit_one(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_missing_file_is_empty_state(self, tmp_path):
        session_names.configure(tmp_path / "none.json")
        assert session_names.select_for_push([_rec("s", "n")])

    @pytest.mark.parametrize(
        "content",
        ["", "not json", "[]", '{"version": 99}', '{"version": 1, "names": [], "files": 3}'],
    )
    def test_a_corrupt_file_is_tolerated(self, tmp_path, content):
        path = tmp_path / "state.json"
        path.write_text(content)
        path.chmod(0o600)
        session_names.configure(path)
        assert session_names.select_for_push([_rec("s", "n")])

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
    def test_a_file_others_can_write_is_not_trusted(self, tmp_path):
        path = tmp_path / "state.json"
        self._commit_one(path)
        path.chmod(0o666)
        session_names.clear_state()
        session_names.configure(path)
        assert session_names.select_for_push([_rec("secret-session", "secret-name")])
        session_names.commit([])
        session_names.save()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_a_symlinked_file_is_not_trusted(self, tmp_path):
        real = tmp_path / "real.json"
        self._commit_one(real)
        link = tmp_path / "link.json"
        link.symlink_to(real)
        session_names.clear_state()
        session_names.configure(link)
        assert session_names.select_for_push([_rec("secret-session", "secret-name")])

    def test_the_state_stays_bounded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(session_names, "_STATE_LIMIT", 5)
        path = tmp_path / "state.json"
        session_names.configure(path)
        session_names.commit(session_names.select_for_push([_rec(f"s{i}", "n") for i in range(12)]))
        assert len(json.loads(path.read_text())["names"]) == 5
        session_names.clear_state()
        path.write_text(
            json.dumps({"version": 1, "names": {f"k{i}": "v" for i in range(9)}, "files": {}})
        )
        path.chmod(0o600)
        session_names.configure(path)
        assert len(session_names._sent) == 5

    def test_an_unwritable_path_does_not_raise(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        session_names.configure(blocker / "state.json")
        session_names.commit(session_names.select_for_push([_rec("s", "n")]))

    def test_an_unchanged_state_is_not_rewritten(self, tmp_path):
        path = tmp_path / "state.json"
        self._commit_one(path)
        before = path.stat().st_mtime_ns
        session_names.commit(session_names.select_for_push([_rec("secret-session", "secret-name")]))
        assert path.stat().st_mtime_ns == before


# --- sidecar integration ---------------------------------------------------


def _write(path: Path, lines: list[dict]) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    """A runner whose collection reads fake transcripts and whose pushes are recorded."""
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))
    monkeypatch.setattr(sidecar, "_post_credential_manifest", lambda **kw: None)
    monkeypatch.setattr(sidecar, "custom_login_dirs", lambda *a: [])
    monkeypatch.setattr(
        sidecar, "_discover_anthropic_log_paths", lambda: sorted(projects.glob("*.jsonl"))
    )
    monkeypatch.setattr(sidecar, "_extract_events_for_provider", lambda **kw: 0)
    monkeypatch.setattr(sidecar, "queue_flush", lambda *a, **k: 0)
    monkeypatch.setattr(sidecar, "queue_push", lambda payload, *a: True)

    posts: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {"ok": True, "reply": {"names_upserted": 1}}

    def post(url, payload, *a, **k):
        posts.append(payload)
        return (True, outcome["reply"], 200) if outcome["ok"] else (False, "offline", 0)

    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", post)
    runner = sidecar.DaemonRunner({"api_url": "http://localhost", "api_key": "test"})

    class Cycle:
        dir = projects

        def run(self) -> list[dict[str, Any]]:
            posts.clear()
            runner.run_once(providers=["anthropic"])
            return [n for p in posts for n in (p.get("session_names") or [])]

        fail = outcome
        sent = posts

    return Cycle()


def test_names_are_sent_once_and_a_rename_sends_only_the_new_name(cycle):
    transcript = cycle.dir / "sess-1.jsonl"
    _write(
        transcript,
        [
            {"type": "agent-name", "agentName": "placeholder-a", "sessionId": "sess-1"},
            {"type": "ai-title", "aiTitle": "Placeholder title", "sessionId": "sess-1"},
        ],
    )
    first = cycle.run()
    assert {(n["kind"], n["name"]) for n in first} == {
        ("agent", "placeholder-a"),
        ("generated", "Placeholder title"),
    }
    assert cycle.run() == []
    with transcript.open("a") as f:
        f.write(
            json.dumps({"type": "agent-name", "agentName": "placeholder-b", "sessionId": "sess-1"})
            + "\n"
        )
    third = cycle.run()
    assert [(n["kind"], n["name"], n["session_id"]) for n in third] == [
        ("agent", "placeholder-b", "sess-1")
    ]
    assert cycle.run() == []


def test_a_failed_push_does_not_mark_names_sent(cycle):
    _write(
        cycle.dir / "sess-2.jsonl",
        [{"type": "ai-title", "aiTitle": "Placeholder title", "sessionId": "sess-2"}],
    )
    cycle.fail["ok"] = False
    assert len(cycle.run()) == 1
    cycle.fail["ok"] = True
    assert len(cycle.run()) == 1
    assert cycle.run() == []


def test_names_ride_the_first_batch_only_and_stay_under_the_cap(cycle, monkeypatch):
    for i in range(3):
        _write(
            cycle.dir / f"sess-{i}.jsonl",
            [{"type": "ai-title", "aiTitle": f"Placeholder {i}", "sessionId": f"sess-{i}"}],
        )
    events = [{"event_id": str(i), "provider_id": "anthropic"} for i in range(2500)]
    real_extract = sidecar._extract_events_for_provider

    def extract(**kw):
        kw["out_events"].extend(events)
        return real_extract(**kw)

    monkeypatch.setattr(sidecar, "_extract_events_for_provider", extract)
    cycle.run()
    batches = cycle.sent
    assert len(batches) == 3
    assert len(batches[0]["session_names"]) == 3
    assert all(not b["session_names"] for b in batches[1:])
    assert all(len(b["session_names"] or []) <= session_names.MAX_PER_PUSH for b in batches)


def test_names_are_never_logged(cycle, caplog):
    _write(
        cycle.dir / "sess-3.jsonl",
        [
            {"type": "agent-name", "agentName": "private-agent-name", "sessionId": "sess-3"},
            {"type": "ai-title", "aiTitle": "Private generated title", "sessionId": "sess-3"},
        ],
    )
    with caplog.at_level(logging.DEBUG):
        cycle.run()
        cycle.run()
    text = caplog.text
    assert "private-agent-name" not in text
    assert "Private generated title" not in text


def test_a_failing_collector_does_not_break_the_cycle(cycle, monkeypatch, caplog):
    def boom(out):
        raise RuntimeError("private-agent-name")

    monkeypatch.setitem(sidecar._SESSION_NAME_COLLECTORS, "anthropic", boom)
    with caplog.at_level(logging.DEBUG):
        assert cycle.run() == []
    assert "private-agent-name" not in caplog.text
    assert cycle.sent  # the push still happened


def test_a_200_without_names_upserted_does_not_mark_names_sent(cycle, monkeypatch):
    _write(
        cycle.dir / "sess-4.jsonl",
        [{"type": "ai-title", "aiTitle": "Placeholder title", "sessionId": "sess-4"}],
    )
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **k: (True, {}, 200))
    cycle.run()
    from scripts.sidecar_pkg import session_names as sn

    pending = sn.select_for_push(
        [sn.make_record("anthropic", "sess-4", "Placeholder title", "generated")]
    )
    assert len(pending) == 1


def _pending_after(cycle, reply):
    cycle.fail["reply"] = reply
    cycle.run()
    from scripts.sidecar_pkg import session_names as sn

    return sn.select_for_push(
        [sn.make_record("anthropic", "sess-5", "Placeholder title", "generated")]
    )


def _title_file(cycle):
    _write(
        cycle.dir / "sess-5.jsonl",
        [{"type": "ai-title", "aiTitle": "Placeholder title", "sessionId": "sess-5"}],
    )


def test_names_error_leaves_names_unsent_for_the_next_cycle(cycle):
    _title_file(cycle)
    assert len(_pending_after(cycle, {"names_upserted": 0, "names_error": True})) == 1
    assert [n["name"] for n in cycle.run()] == ["Placeholder title"]  # sent again


def test_names_error_false_commits_the_names(cycle):
    _title_file(cycle)
    assert _pending_after(cycle, {"names_upserted": 1, "names_error": False}) == []


def test_older_server_without_names_error_still_commits(cycle):
    _title_file(cycle)
    assert _pending_after(cycle, {"names_upserted": 1}) == []


def test_the_name_cap_matches_the_servers():
    from app.models.schemas import SESSION_NAME_MAX_LENGTH

    assert session_names.MAX_NAME_LENGTH == SESSION_NAME_MAX_LENGTH
