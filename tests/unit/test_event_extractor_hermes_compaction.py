"""Unit tests for Hermes Agent watermark log writes, compaction, and migration."""

import json
from datetime import UTC, datetime
from typing import Any, get_args

from scripts.sidecar_pkg.event_extractors import hermes as hermes_extractor
from tests.unit._event_extractor_hermes_helpers import (
    _bump_usage,
    _make_db,
    _parse,
    _read_watermark,
    _watermark_entries,
    _watermark_keys,
    requires_pipe_in_path,
)

# ---------------------------------------------------------------------------
# The watermark log (#959)
# ---------------------------------------------------------------------------


def test_the_database_path_is_stored_once_per_database(tmp_path):
    """The path is the group the slices live in, not part of every slice.

    It used to be repeated in every key of the flat file, which is where most
    of the bytes of a large watermark went.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        content = state_file.read_text(encoding="utf-8")
        # The path is counted in the form the file holds it in: JSON escapes a
        # backslash, so on Windows the file reads "C:\\Users\\..." where the
        # path itself is "C:\Users\...". The writer escapes it the same way --
        # json.dumps with the default ensure_ascii=True, in
        # _compact_hermes_watermark (sidecar_pkg/hermes_watermark.py) -- so
        # this has to keep matching it.
        stored = json.dumps(str(db_path.resolve()))[1:-1]
        assert content.count(stored) == 1
        # Both slices are still there.
        assert len(_watermark_keys(state_file)) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_a_cycle_writes_only_the_slices_that_changed(tmp_path):
    """The log grows by what changed, not by the whole state.

    The old format rewrote the whole file on every cycle, so the bytes
    written per day grew with the file.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        size = state_file.stat().st_size

        # A cycle that has nothing new to report writes nothing at all.
        _parse(db_path, state_file)
        assert state_file.stat().st_size == size

        # A cycle that changes one slice appends one line with that slice.
        _bump_usage(db_path, "api-sess-kimi-01", input_tokens=5000)
        _parse(db_path, state_file)
        lines = state_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        appended = json.loads(lines[-1])
        slices = appended["accounts"]["default"]
        assert len(slices) == 1
        assert any("api-sess-kimi-01" in key for key in slices)
    finally:
        db_path.unlink(missing_ok=True)


def test_the_log_is_compacted_when_it_grows_past_the_state(monkeypatch, tmp_path):
    """The log cannot grow without bound.

    Every cycle appends a line; once the log holds more lines than the state
    it carries, the state is the cheaper file to write, so the log is folded
    back into it -- with every mark still in it.
    """
    monkeypatch.setattr(hermes_extractor, "WATERMARK_COMPACT_MIN_LINES", 1)
    db_one, _ = _make_db(tmp_path)
    db_two, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse([db_one, db_two], state_file)
        cycles = 4
        for _ in range(cycles):
            _bump_usage(db_one, "api-sess-kimi-01", input_tokens=5000)
            reported = _parse([db_one, db_two], state_file)
            # Only the session that moved is reported. A mark that the
            # compaction lost would be reported again from a lost baseline,
            # and the session's whole lifetime with it.
            assert len(reported) == 1
            assert reported[0].session_id == "api-sess-kimi-01"

        lines = state_file.read_text(encoding="utf-8").splitlines()
        # The log was folded back into one line per database, so it holds
        # fewer lines than the cycles appended.
        assert len(lines) < cycles
        # And every mark is still in it -- both databases.
        assert len(_watermark_keys(state_file)) == 4
        marks = _watermark_entries(state_file)
        kimi = next(mark for key, mark in marks.items() if "api-sess-kimi-01" in key)
        assert kimi["input_tokens"] == 50000 + cycles * 5000
    finally:
        db_one.unlink(missing_ok=True)
        db_two.unlink(missing_ok=True)


def test_a_forgotten_slice_is_written_to_the_log(tmp_path):
    """The log says a slice is gone, so the next cycle does not put it back."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    try:
        _parse(db_path, state_file)
        db_path.unlink()
        _parse(db_path, state_file)

        lines = state_file.read_text(encoding="utf-8").splitlines()
        forgotten = json.loads(lines[-1])["accounts"]["default"]
        assert forgotten
        assert all(mark is None for mark in forgotten.values())
        assert _watermark_keys(state_file) == []
    finally:
        db_path.unlink(missing_ok=True)


def test_a_damaged_line_in_the_log_is_ignored(tmp_path):
    """A crash during the write can leave a torn last line.

    The log is a cache: skipping the line beats refusing to read the file,
    which would lose every mark in it.
    """
    state_file = tmp_path / "hermes_watermark.jsonl"
    state_file.write_text(
        json.dumps({"db": "/db", "accounts": {"default": {"s": {"input_tokens": 1}}}})
        + "\n"
        + '{"db": "/db", "accounts": {"default": {"t": {"input_to'
        + "\n"
        + json.dumps({"db": "/db", "accounts": {"default": {"u": {"input_tokens": 3}}}})
        + "\n",
        encoding="utf-8",
    )

    state = _read_watermark(state_file)
    assert list(state) == ["/db"]
    assert state["/db"]["default"]["s"]["input_tokens"] == 1
    # The lines after the damaged one are still read.
    assert state["/db"]["default"]["u"]["input_tokens"] == 3


def test_the_legacy_watermark_is_migrated_without_losing_a_mark(tmp_path):
    """The flat legacy file is read once and written in the new format.

    A mark is the difference between what the sidecar already reported and
    what it would report next: a mark that is lost is a session whose whole
    lifetime is reported a second time.
    """
    legacy = tmp_path / "hermes_watermark.json"
    # A database that exists, so the marks are not reclaimed right away.
    db = str(tmp_path)
    legacy.write_text(
        json.dumps(
            {
                f"default|{db}|default|sess-1|model-1|prov-1||mode-1|task-1": {
                    "input_tokens": 100,
                    "emission_seq": 3,
                    "last_seen": 1780000000.0,
                },
                f"other@host|{db}|default|sess-2|model-2|prov-2||mode-2|task-2": {
                    "input_tokens": 200,
                    "emission_seq": 1,
                    "last_seen": 1780000000.0,
                },
            }
        ),
        encoding="utf-8",
    )
    state_file = tmp_path / "hermes_watermark.jsonl"

    _parse([], state_file)

    state = _read_watermark(state_file)
    accounts = state[db]
    assert (
        accounts["default"]["default|sess-1|model-1|prov-1||mode-1|task-1"]["input_tokens"] == 100
    )
    assert (
        accounts["other@host"]["default|sess-2|model-2|prov-2||mode-2|task-2"]["input_tokens"]
        == 200
    )
    # The legacy file is kept, so a reverted sidecar still finds it.
    assert legacy.exists()


def test_the_migration_reads_the_account_of_an_account_id_that_holds_a_pipe(tmp_path):
    """An account id that holds a ``|`` does not shift the fields.

    The account is an opaque string, and one that holds a ``|`` would shift
    every field behind it if the key were split on the ``|``.
    """
    legacy = tmp_path / "hermes_watermark.json"
    # A database that exists, so the marks are not reclaimed right away.
    db = str(tmp_path)
    legacy.write_text(
        json.dumps(
            {
                f"op|tag|{db}|default|sess|model|prov||mode|task": {
                    "input_tokens": 100,
                    "emission_seq": 3,
                    "last_seen": 1780000000.0,
                }
            }
        ),
        encoding="utf-8",
    )
    state_file = tmp_path / "hermes_watermark.jsonl"

    _parse([], state_file)

    state = _read_watermark(state_file)
    accounts = state[db]
    assert "op|tag" in accounts
    assert accounts["op|tag"]["default|sess|model|prov||mode|task"]["input_tokens"] == 100


def test_the_migration_drops_a_key_it_cannot_read(tmp_path):
    """A key without a database path is not the mark of any slice.

    It cannot be matched to a row, so it protects nothing -- and the legacy
    file keeps it.
    """
    legacy = tmp_path / "hermes_watermark.json"
    # A database that exists, so the marks are not reclaimed right away.
    db = str(tmp_path)
    legacy.write_text(
        json.dumps(
            {
                "default|not-a-path|default|sess|model|hermes|||": {"last_seen": 1.0},
                f"default|{db}|default|sess|model|hermes|||": {"last_seen": 2.0},
            }
        ),
        encoding="utf-8",
    )
    state_file = tmp_path / "hermes_watermark.jsonl"

    _parse([], state_file)

    state = _read_watermark(state_file)
    assert list(state) == [db]
    assert legacy.exists()


@requires_pipe_in_path
def test_the_migration_matches_the_longest_discovered_path_first(tmp_path):
    """A path that is a prefix of another one must not win the match.

    The discovered databases are matched against the key as whole strings.
    "/data/a|b" is a prefix of "/data/a|b|c", and the shorter one matches
    the longer key as well -- the mark would be filed under a database that
    does not exist, be reclaimed as a dead slice, and the session's whole
    lifetime would be reported a second time.

    The ``|`` inside the path is what puts the two in the same shape: the
    legacy key is cut on the ``|``, so the key of "/data/a|b|c" also reads
    as "/data/a". Rename the two directories to "a" and "ab" and this test
    would stay green while the regression it guards comes back.
    """
    short_db = tmp_path / "a|b"
    long_db = tmp_path / "a|b|c"
    short_db.mkdir()
    long_db.mkdir()
    legacy = tmp_path / "hermes_watermark.json"
    legacy.write_text(
        json.dumps(
            {
                f"default|{long_db.resolve()}|default|sess|model|prov||mode|task": {
                    "input_tokens": 100,
                    "emission_seq": 3,
                    "last_seen": datetime.now(UTC).timestamp(),
                }
            }
        ),
        encoding="utf-8",
    )
    state_file = tmp_path / "hermes_watermark.jsonl"

    _parse([short_db, long_db], state_file)

    state = _read_watermark(state_file)
    assert list(state) == [str(long_db.resolve())]
    assert (
        state[str(long_db.resolve())]["default"]["default|sess|model|prov||mode|task"][
            "input_tokens"
        ]
        == 100
    )


def test_the_split_matches_the_longest_discovered_path_first():
    """The longest-first rule, on path strings rather than real directories.

    A Windows path cannot hold a ``|``, so the test above cannot create its
    directories there. The split matches whole strings against the key, so
    the two paths need not exist: "/data/a|b" is a prefix of "/data/a|b|c"
    and both read the same legacy key.
    """
    short, long = "/data/a|b", "/data/a|b|c"
    key = f"default|{long}|default|sess|model|prov||mode|task"
    expected = (long, "default", "default|sess|model|prov||mode|task")
    # Discovery order must not decide the match.
    assert hermes_extractor._split_legacy_watermark_key(key, "default", [short, long]) == expected
    assert hermes_extractor._split_legacy_watermark_key(key, "default", [long, short]) == expected


def test_a_deleted_watermark_log_is_not_refilled_with_the_legacy_baseline(tmp_path):
    """Deleting the log re-scans; it does not restore the legacy baseline.

    The legacy file stays on disk (a reverted sidecar still finds it), so
    without a marker that records the one-time read, the migration would
    run again whenever the log was deleted and the documented re-scan would
    silently do nothing on every upgraded install.
    """
    legacy = tmp_path / "hermes_watermark.json"
    db = str(tmp_path)
    legacy.write_text(
        json.dumps(
            {
                f"default|{db}|default|sess|model|prov||mode|task": {
                    "input_tokens": 100,
                    "emission_seq": 3,
                    "last_seen": datetime.now(UTC).timestamp(),
                }
            }
        ),
        encoding="utf-8",
    )
    state_file = tmp_path / "hermes_watermark.jsonl"

    _parse([], state_file)

    # The first run migrated the legacy file and recorded that it did.
    assert _read_watermark(state_file)[db]["default"]
    marker = tmp_path / "hermes_watermark.json.migrated"
    assert marker.exists()
    # The legacy file is still there, for a sidecar that is reverted.
    assert legacy.exists()

    # A deleted log leaves an empty state: the re-scan starts from zero.
    state_file.unlink()
    _parse([], state_file)

    assert _read_watermark(state_file) == {}


def test_the_watermark_changes_alias_types_the_slice_level():
    """The alias names the slice level, so a mark can be ``None``.

    ``dict[Any | None]`` is a one-argument alias: it evaluates, but it
    types nothing, and mypy does not check ``scripts/``.
    """
    assert get_args(hermes_extractor.WatermarkChanges) == (
        str,
        dict[str, dict[str, Any | None]],
    )
