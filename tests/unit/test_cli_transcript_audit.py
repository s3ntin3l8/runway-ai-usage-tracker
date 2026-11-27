"""Mid-history rewrites of Claude Code CLI transcripts (#1407), via poll_anthropic_events."""

import json
import os
from datetime import UTC, datetime

import pytest

from scripts.sidecar_pkg import transcript_audit
from scripts.sidecar_pkg.event_extractors import anthropic
from scripts.sidecar_pkg.event_extractors.anthropic import poll_anthropic_events
from scripts.sidecar_pkg.event_watermark import EventWatermark

SINCE = datetime(2026, 1, 1, tzinfo=UTC)
PAD = json.dumps({"type": "user", "padding": "x" * 4000}) + "\n"
CHUNK = transcript_audit.CHUNK_BYTES


@pytest.fixture(autouse=True)
def _fresh_process():
    anthropic._AUDIT_PROGRESS.clear()
    yield
    anthropic._AUDIT_PROGRESS.clear()


def walk(path):
    return anthropic._AUDIT_PROGRESS[("a@x.io", str(path.resolve()))]["fields"]


def record(mid, ts="2026-07-01T12:00:00Z"):
    return (
        json.dumps(
            {
                "type": "assistant",
                "timestamp": ts,
                "sessionId": "cli-one",
                "requestId": f"req-{mid}",
                "message": {
                    "id": mid,
                    "model": "claude-sonnet-4-5-20250929",
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                },
            }
        )
        + "\n"
    )


def transcript(tmp_path, name="s.jsonl", pads=120):
    path = tmp_path / "projects" / "p" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PAD * pads + record("mid-a") + PAD * pads + record("end"))
    return path


def poll(wm, paths, **kwargs):
    events = poll_anthropic_events(list(paths), "a@x.io", SINCE, wm, **kwargs)
    wm.commit()
    return {e.event_id.split("|")[0] for e in events}


def rewrite_middle(path, *, append=""):
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b'"mid-a"', b'"mid-b"') + append.encode())
    if not append:
        try:
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        except OSError:
            pytest.skip("mtime not settable on this filesystem")


def cursor(wm_path, path):
    return json.loads(wm_path.read_text())["anthropic_transcripts"][str(path.resolve())]


def test_a_growing_transcript_with_a_middle_edit_replays_the_edit(tmp_path):
    path = transcript(tmp_path)
    wm_path = tmp_path / "wm.json"
    wm = EventWatermark(wm_path)
    assert poll(wm, [path]) == {"mid-a", "end"}
    assert poll(wm, [path]) == set()
    rewrite_middle(path, append=record("tail"))
    assert {"mid-b", "tail"} <= poll(wm, [path])


def test_a_cursor_without_chunks_resumes_and_is_backfilled(tmp_path):
    path = transcript(tmp_path)
    wm_path = tmp_path / "wm.json"
    poll(EventWatermark(wm_path), [path])
    data = json.loads(wm_path.read_text())
    for c in data["anthropic_transcripts"].values():
        for key in ("chunks", *transcript_audit.AUDIT_FIELDS):
            c.pop(key, None)  # a cursor written before #1407
    wm_path.write_text(json.dumps(data))
    assert poll(EventWatermark(wm_path), [path]) == set()
    chunks = cursor(wm_path, path)["chunks"]
    assert chunks and all(isinstance(c, str) for c in chunks)


def test_idle_audit_advances_across_scans_and_finds_a_same_size_edit(tmp_path):
    path = transcript(tmp_path)
    wm_path = tmp_path / "wm.json"
    poll(EventWatermark(wm_path), [path])
    data = json.loads(wm_path.read_text())
    (c,) = data["anthropic_transcripts"].values()
    count = len(c["chunks"])
    assert count >= 4
    c["chunks"] = [None] * count
    wm_path.write_text(json.dumps(data))
    for scan in range(1, count + 1):
        assert poll(EventWatermark(wm_path), [path], audit_bytes=CHUNK) == set()
        assert sum(x is not None for x in cursor(wm_path, path)["chunks"]) == scan
    rewrite_middle(path)
    found = set()
    for _ in range(count):
        found |= poll(EventWatermark(wm_path), [path], audit_bytes=CHUNK)
        if found:
            break
    assert "mid-b" in found


def test_audit_reads_at_most_its_budget_per_scan(tmp_path, monkeypatch):
    paths = [transcript(tmp_path, f"s{i}.jsonl", pads=60) for i in range(3)]
    wm_path = tmp_path / "wm.json"
    poll(EventWatermark(wm_path), paths)
    reads = []
    real = transcript_audit.chunk_digest
    monkeypatch.setattr(
        transcript_audit, "chunk_digest", lambda f, i, end: reads.append(i) or real(f, i, end)
    )
    for _ in range(3):
        before = {str(p): walk(p)["audit_next"] if anthropic._AUDIT_PROGRESS else 0 for p in paths}
        reads.clear()
        assert poll(EventWatermark(wm_path), paths, audit_bytes=3 * CHUNK) == set()
        assert len(reads) == 3
        assert all(walk(p)["audit_next"] != before[str(p)] for p in paths)


def test_an_idle_transcript_does_not_rewrite_the_watermark_file(tmp_path):
    path = transcript(tmp_path)
    wm_path = tmp_path / "wm.json"
    poll(EventWatermark(wm_path), [path])
    poll(EventWatermark(wm_path), [path])  # chunks already known: nothing to stage
    written = wm_path.stat().st_mtime_ns, wm_path.read_bytes()
    for _ in range(3):
        assert poll(EventWatermark(wm_path), [path], audit_bytes=CHUNK) == set()
    assert (wm_path.stat().st_mtime_ns, wm_path.read_bytes()) == written
    assert walk(path)["audit_done"] == 3  # the walk still advanced, in memory


def test_a_transcript_larger_than_the_budget_completes_its_pass(tmp_path):
    path = transcript(tmp_path, pads=400)
    small = transcript(tmp_path, "small.jsonl", pads=10)
    wm_path = tmp_path / "wm.json"
    poll(EventWatermark(wm_path), [path, small])
    count = len(cursor(wm_path, path)["chunks"])
    budget = 2 * CHUNK
    assert count * CHUNK > 3 * budget
    for _ in range(count):
        assert poll(EventWatermark(wm_path), [path, small], audit_bytes=budget) == set()
    assert "audited" in walk(path)
    assert "audited" in walk(small)
    # The walk reaches the file with the next cursor written for the transcript.
    with path.open("a") as f:
        f.write(record("tail"))
    assert poll(EventWatermark(wm_path), [path, small], audit_bytes=budget) == {"tail"}
    assert "audited" in cursor(wm_path, path)
