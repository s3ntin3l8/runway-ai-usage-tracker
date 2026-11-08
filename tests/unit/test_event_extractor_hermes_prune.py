"""Unit tests for Hermes Agent watermark pruning and live database row checks."""

import json
import os
import sqlite3
from datetime import UTC, datetime

from scripts.sidecar_pkg.event_extractors import hermes as hermes_extractor
from scripts.sidecar_pkg.event_extractors.hermes import (
    WATERMARK_PRUNE_IDLE_DAYS,
    _prune_stale_watermark_slices,
)
from tests.fixtures.hermes_fixture import make_hermes_db
from tests.unit._event_extractor_hermes_helpers import (
    _cutoff,
    _delete_usage_rows,
    _make_db,
    _parse,
    _read_watermark,
    _set_row_times,
    _slice_count,
    _watermark_keys,
    requires_pipe_in_path,
)

# ---------------------------------------------------------------------------
# The prune (#926, #958)
# ---------------------------------------------------------------------------


def test_prune_drops_the_slices_of_a_vanished_database(tmp_path):
    """A database that is gone leaves its high-water marks behind for good."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        # The database is deleted: its slices are now dead weight.
        db_path.unlink()
        state = _read_watermark(state_file)
        pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
        assert len(pruned) == 2
        assert state == {}
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_keeps_a_recently_used_slice_of_a_vanished_database(tmp_path):
    """The database may only be *temporarily* gone (a backup renamed it aside).

    A session that was active recently keeps its mark, so the sidecar still
    knows what it already reported when the file comes back.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"
    now = datetime.now(UTC).timestamp()

    try:
        _set_row_times(db_path, last_seen=now)
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        db_path.unlink()
        state = _read_watermark(state_file)
        pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
        assert pruned == []
        assert _slice_count(state) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_keeps_the_slices_of_a_live_database(tmp_path):
    """A database that exists keeps every slice, however old the session is."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        state = _read_watermark(state_file)
        pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
        assert pruned == []
        assert _slice_count(state) == 2
    finally:
        db_path.unlink(missing_ok=True)


@requires_pipe_in_path
def test_prune_keeps_the_slices_of_a_database_whose_path_holds_a_pipe(tmp_path):
    """A database whose path holds a ``|`` is no longer cut short.

    The path used to be part of every key, and the key was split on the
    ``|``, so the path of such a database was truncated. The path is now
    the group the slices live in, so it is stored whole.
    """
    db_path = tmp_path / "pipe|name.db"
    conn = make_hermes_db(str(db_path))
    conn.close()
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2
        assert str(db_path.resolve()) in _read_watermark(state_file)

        # The file is gone: the slices are reclaimed.
        db_path.unlink()
        state = _read_watermark(state_file)
        pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
        assert len(pruned) == 2
        assert state == {}
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_reclaims_a_pipe_path_database_that_is_gone(monkeypatch, tmp_path):
    """The pipe-in-path prune, on a path string rather than a real file.

    A Windows path cannot hold a ``|``, so the test above cannot create its
    database there. The prune only ever sees the path as a string and asks
    whether the file exists, so a path that was never created exercises the
    same rule on every platform.

    Windows also refuses to stat such a path: it raises WinError 123, not
    FileNotFoundError, and "I cannot tell" keeps the slices. So the stat of
    this one path answers "not found", as it does for a POSIX path.
    """
    gone = str(tmp_path / "pipe|name.db")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if path == gone:
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    state = {
        gone: {
            "default": {
                "sess|model": {"input_tokens": 1, "last_seen": 1.0},
            }
        }
    }
    pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
    assert pruned == [(gone, "default", "sess|model")]
    assert state == {}


def test_prune_is_written_out_even_when_the_cycle_emits_nothing(tmp_path):
    """A cycle that reports nothing can still shrink the file, and it must."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        db_path.unlink()
        events = _parse(db_path, state_file)
        assert events == []
        assert _watermark_keys(state_file) == []
    finally:
        db_path.unlink(missing_ok=True)


def test_a_reappearing_database_does_not_double_count(tmp_path):
    """A database that comes back must report the delta, not its whole life.

    This is the regression test for the idle guard: a prune that dropped the
    mark of a recently used session would make the sidecar report the
    session's entire lifetime as new usage, under an event id the server has
    never seen (the emission counter restarts), and the server would count it
    twice.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"
    now = datetime.now(UTC).timestamp()

    try:
        _set_row_times(db_path, last_seen=now)
        first = _parse(db_path, state_file)
        assert len(first) == 2

        # The file disappears (a backup renamed it aside) and the sidecar
        # reports nothing while it is gone.
        db_path.unlink()
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        # It comes back and the session moved on.
        conn = make_hermes_db(str(db_path))
        conn.execute(
            """
            UPDATE session_model_usage
            SET input_tokens = input_tokens + 5000,
                output_tokens = output_tokens + 300,
                api_call_count = 11,
                first_seen = ?,
                last_seen = ?
            WHERE session_id = 'api-sess-kimi-01'
            """,
            (now, now),
        )
        conn.commit()
        conn.close()

        again = _parse(db_path, state_file)
        assert len(again) == 1
        delta = again[0]
        assert delta.session_id == "api-sess-kimi-01"
        # The delta, not the session's whole lifetime (50000 + 5000).
        assert delta.tokens_input == 5000
        assert delta.tokens_output == 300
        # The emission counter continues, so the event id stays one the server
        # has already seen for this slice.
        assert delta.event_id.endswith("|c11s2")
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_keeps_the_marks_of_an_account_id_that_holds_a_pipe(tmp_path):
    """The account is a level of the state, so it is never split.

    The account id may be an opaque string, and one that holds a ``|`` would
    shift every field of the old flat key.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"
    account = "operator|tag"

    try:
        _parse(db_path, state_file, account_id=account)
        assert len(_watermark_keys(state_file)) == 2
        assert account in _read_watermark(state_file)[str(db_path.resolve())]

        # The path is read correctly, so the live database keeps its slices.
        state = _read_watermark(state_file)
        pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
        assert pruned == []
        assert _slice_count(state) == 2

        # And once the file is gone, the slices are reclaimed.
        db_path.unlink()
        state = _read_watermark(state_file)
        pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
        assert len(pruned) == 2
        assert state == {}
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_applies_the_documented_idle_window(tmp_path):
    """The grace window the extractor documents is the one it applies.

    The boundary is checked through the real parse path, so the window the
    implementation uses is pinned rather than the constant alone.
    """
    now = datetime.now(UTC).timestamp()
    # The literal, not the constant: a test that derives its window from
    # WATERMARK_PRUNE_IDLE_DAYS moves with it, so dropping the constant to a
    # day would keep this green while the docs still say 30.
    assert WATERMARK_PRUNE_IDLE_DAYS == 30
    window = 30 * 86400

    def _keys_after_prune(idle_seconds: float) -> list[str]:
        """Write a slice idle for ``idle_seconds``, drop its file, prune."""
        state_file = tmp_path / f"hermes_watermark_{int(idle_seconds)}.jsonl"
        db_path, _ = _make_db(tmp_path)
        try:
            _set_row_times(db_path, last_seen=now - idle_seconds)
            _parse(db_path, state_file)
            assert len(_watermark_keys(state_file)) == 2
            db_path.unlink()
            _parse(db_path, state_file)
            return _watermark_keys(state_file)
        finally:
            db_path.unlink(missing_ok=True)

    # Quiet for less than the window: the mark stays.
    assert len(_keys_after_prune(window - 3600)) == 2
    # Quiet for longer than the window: it is reclaimed.
    assert _keys_after_prune(window + 3600) == []


def test_prune_keeps_a_mark_whose_timestamp_cannot_be_read(tmp_path):
    """An unreadable timestamp is kept, and it never fails the collection.

    Every other unreadable thing here is kept rather than guessed; a value
    that raises while it is read has to be treated the same way, instead of
    taking the whole Hermes collection down with it.
    """
    state_file = tmp_path / "hermes_watermark.jsonl"
    state_file.write_text(
        json.dumps(
            {
                "db": "/tmp/gone-one/state.db",
                "accounts": {
                    "default": {
                        "default|sess|model|hermes|||": {"last_seen": "2026-01-01T00:00:00Z"}
                    },
                    "other": {"default|sess|model|hermes|||": {"last_seen": 1.0}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    state = _read_watermark(state_file)
    pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
    # Only the readable, idle one is reclaimed.
    assert len(pruned) == 1
    assert len(state) == 1

    # And the same file does not take the collection down.
    events = _parse([], state_file)
    assert events == []
    assert len(_watermark_keys(state_file)) == 1


# ---------------------------------------------------------------------------
# The row check: a live database's deleted rows (#958)
# ---------------------------------------------------------------------------


def test_a_deleted_row_of_a_live_database_is_reclaimed(tmp_path):
    """A session Hermes deleted leaves a tombstone behind in the watermark.

    The database is still there, so the file is not gone -- but the row of the
    deleted session is, and a row that is gone can never advance again. Its
    high-water mark is dead weight, and the watermark of a long-lived install
    grows with every session that ever ran unless it is reclaimed.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        # Hermes drops one of the sessions.
        _delete_usage_rows(db_path, "api-sess-kimi-01")

        _parse(db_path, state_file)
        keys = _watermark_keys(state_file)
        assert len(keys) == 1
        assert all("api-sess-kimi-01" not in key for key in keys)
    finally:
        db_path.unlink(missing_ok=True)


def test_a_recently_used_row_that_is_gone_keeps_its_mark(tmp_path):
    """A session that was active recently keeps its mark.

    Its row is gone, so it can never advance -- but a session can come back
    (Hermes re-creates the row of a session it still holds, with the counters
    it kept). Dropping the mark of a session that was quiet for less than the
    idle window would make the sidecar report that session's whole lifetime
    as new usage.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"
    now = datetime.now(UTC).timestamp()

    try:
        _set_row_times(db_path, last_seen=now)
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        _delete_usage_rows(db_path, "api-sess-kimi-01")

        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_a_row_that_comes_back_is_not_counted_twice(tmp_path):
    """A session whose row comes back reports its delta, not its whole life.

    This is the regression test for the idle guard of the row check: the row
    is dropped by Hermes and comes back with the counters it had. The mark of
    a session that was quiet for less than the idle window has to survive, or
    the sidecar reports the session's entire lifetime as new usage under an
    event id the server has never seen (the emission counter restarts) and the
    server counts it twice.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"
    now = datetime.now(UTC).timestamp()

    try:
        _set_row_times(db_path, last_seen=now)
        first = _parse(db_path, state_file)
        assert len(first) == 2

        # The session is dropped from Hermes.
        _delete_usage_rows(db_path, "api-sess-kimi-01")
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        # It comes back and moves on.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            INSERT INTO session_model_usage (
                session_id, model, billing_provider, billing_base_url, billing_mode,
                task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
                first_seen, last_seen
            ) VALUES (
                'api-sess-kimi-01', 'kimi-for-coding', 'kimi-coding',
                'https://api.kimi.com/coding/v1/', '', '', 11, 55000, 2300, 200000,
                0, 500, 0.0, 0.0, 1780000000.0, ?
            )
            """,
            (now,),
        )
        conn.commit()
        conn.close()

        again = _parse(db_path, state_file)
        assert len(again) == 1
        delta = again[0]
        assert delta.session_id == "api-sess-kimi-01"
        # The delta, not the session's whole lifetime (50000 + 5000).
        assert delta.tokens_input == 5000
        assert delta.tokens_output == 300
        # The emission counter continues, so the event id stays one the server
        # has already seen for this slice.
        assert delta.event_id.endswith("|c11s2")
    finally:
        db_path.unlink(missing_ok=True)


def test_the_rows_of_two_databases_are_told_apart(tmp_path):
    """Two databases can hold the same session id, and each keeps its own.

    The live rows of one database say nothing about the other, so a session
    that is dropped from one of them must not make the other one's slice look
    gone (or alive).
    """
    db_one, _ = _make_db(tmp_path)
    db_two, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse([db_one, db_two], state_file)
        assert len(_watermark_keys(state_file)) == 4

        # The session is dropped from the first database only.
        _delete_usage_rows(db_one, "api-sess-kimi-01")

        _parse([db_one, db_two], state_file)
        keys = _watermark_keys(state_file)
        assert len(keys) == 3
        # The slice of the second database survives: its own row is still there.
        survivors = [key for key in keys if "api-sess-kimi-01" in key]
        assert len(survivors) == 1
        assert str(db_two.resolve()) in survivors[0]
    finally:
        db_one.unlink(missing_ok=True)
        db_two.unlink(missing_ok=True)


def test_a_database_whose_rows_cannot_be_listed_keeps_its_slices(monkeypatch, tmp_path):
    """A database we could not read keeps every slice.

    "I cannot tell" is not "the row is gone": the mark of a session we merely
    failed to look up has to stay, or the next cycle would report its whole
    lifetime as new usage.
    """
    monkeypatch.setattr(hermes_extractor, "_live_slice_suffixes", lambda conn, profile_name: None)
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2

        _delete_usage_rows(db_path, "api-sess-kimi-01")

        _parse(db_path, state_file)
        assert len(_watermark_keys(state_file)) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_the_live_slices_of_an_unreadable_database_cannot_be_told(tmp_path):
    """A database whose rows cannot be listed answers "I cannot tell".

    The answer has to be None rather than an empty set: an empty set says
    "every row is gone", which would have the prune reclaim the marks of the
    sessions that are still there.
    """
    db_path = tmp_path / "no_usage_table.db"
    conn = sqlite3.connect(str(db_path))
    try:
        assert hermes_extractor._live_slice_suffixes(conn, "default") is None
    finally:
        conn.close()


def test_prune_reclaims_only_the_row_that_is_gone(tmp_path):
    """The rule itself: of two slices, the one without a row is reclaimed."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        state = _read_watermark(state_file)
        live = {
            "default|api-sess-minimax-02|MiniMax-M3|minimax-oauth"
            "|https://api.minimax.io/anthropic||background_review"
        }
        pruned = _prune_stale_watermark_slices(
            state,
            idle_cutoff=_cutoff(),
            live_slices={str(db_path.resolve()): live},
        )
        assert len(pruned) == 1
        assert _slice_count(state) == 1
        assert all("api-sess-minimax-02" in key for key in state[str(db_path.resolve())]["default"])
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_keeps_the_slices_of_a_database_that_was_not_discovered(tmp_path):
    """A database that exists but was not discovered keeps every slice.

    Only its own rows can say whether its sessions are still alive, and a
    database the collector did not open is not one it can ask.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        state = _read_watermark(state_file)
        pruned = _prune_stale_watermark_slices(state, idle_cutoff=_cutoff())
        assert pruned == []
        assert _slice_count(state) == 2
    finally:
        db_path.unlink(missing_ok=True)
