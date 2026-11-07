"""scripts/kimi_structure.py reports structure only and survives bad input."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import kimi_structure

SENTINELS = {
    "title": "SENTINEL-TITLE-9f3a",
    "cwd": "/SENTINEL-CWD-71bc/secret",
    "id": "SENTINEL-ID-55de",
    "prompt": "SENTINEL-PROMPT-02aa",
    "workdir": "SENTINEL-WD-4c1f",
}


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "kimi"
    session = root / "sessions" / f"wd_{SENTINELS['workdir']}" / f"session_{SENTINELS['id']}"
    (session / "agents" / "main").mkdir(parents=True)
    (session / "state.json").write_text(
        json.dumps(
            {
                "id": SENTINELS["id"],
                "version": 3,
                "cwd": SENTINELS["cwd"],
                "archived": False,
                "agents": ["main"],
                "custom": {"title": SENTINELS["title"]},
                "createdAt": 1,
                "updatedAt": 2,
                "lastTurnReason": None,
                "isCustomTitle": True,
            }
        )
    )
    records = [
        {"type": "metadata", "id": SENTINELS["id"], "title": SENTINELS["title"]},
        {"type": "turn.prompt", "text": SENTINELS["prompt"]},
        {
            "type": "token_counting.turn_recorded",
            "input": 10,
            "output": 5.5,
            "model": SENTINELS["title"],
            "final": True,
        },
        "not an object",
    ]
    lines = [json.dumps(r) for r in records] + ["{broken"]
    (session / "agents" / "main" / "wire.jsonl").write_text("\n".join(lines) + "\n")
    (root / "session_index.jsonl").write_text(
        json.dumps(
            {
                "sessionDir": SENTINELS["cwd"],
                "sessionId": SENTINELS["id"],
                "workDir": SENTINELS["workdir"],
            }
        )
        + "\n"
    )
    return root


def _run(root: Path, capsys) -> tuple[int, dict, str]:
    code = kimi_structure.main([str(root)])
    out = capsys.readouterr().out
    return code, json.loads(out), out


def test_output_is_structure_only(tmp_path, capsys):
    root = _build(tmp_path)
    code, doc, out = _run(root, capsys)
    assert code == 0
    for sentinel in SENTINELS.values():
        assert sentinel not in out
    assert str(tmp_path) not in out

    keys = doc["state_json"]["keys"]
    assert keys["isCustomTitle"]["types"] == {"boolean": 1}
    assert keys["isCustomTitle"]["boolean_values"] == {"true": 1, "false": 0}
    assert keys["cwd"]["string_length"] == {
        "min": len(SENTINELS["cwd"]),
        "max": len(SENTINELS["cwd"]),
    }
    assert keys["id"]["sessions"] == 1

    wire = doc["wire_jsonl"]
    assert wire["record_types"]["metadata"]["count"] == 1
    assert wire["record_types"]["token_counting.turn_recorded"]["numeric_keys"] == [
        "input",
        "output",
    ]
    assert wire["invalid_lines"] == 1
    assert wire["non_object_lines"] == 1

    located = {(t["file"], t["record_type"], t["key"]): t for t in doc["title_or_name_keys"]}
    meta_title = located[("wire.jsonl", "metadata", "title")]
    assert meta_title["types"] == {"string": 1}
    assert meta_title["string_length"]["max"] == len(SENTINELS["title"])
    assert ("state.json", "state", "title") in located
    assert ("wire.jsonl", "token_counting.turn_recorded", "model") not in located

    index = doc["session_index_jsonl"]["keys"]
    assert set(index) == {"sessionDir", "sessionId", "workDir"}


def test_oversized_line_and_missing_files_are_counted(tmp_path, capsys):
    root = _build(tmp_path)
    wire = next(root.rglob("wire.jsonl"))
    big = '{"type": "turn.prompt", "text": "' + "x" * (kimi_structure.MAX_LINE_BYTES + 10) + '"}'
    wire.write_text(big + "\n" + json.dumps({"type": "metadata"}) + "\n")
    (root / "session_index.jsonl").unlink()
    other = root / "sessions" / "wd_other" / "session_empty"
    other.mkdir(parents=True)

    code, doc, _ = _run(root, capsys)
    assert code == 0
    assert doc["wire_jsonl"]["oversized_lines"] == 1
    assert doc["wire_jsonl"]["record_types"]["metadata"]["count"] == 1
    assert doc["wire_jsonl"]["files_missing_or_unreadable"] == 1
    assert doc["state_json"]["files_missing_or_unreadable"] == 1
    assert doc["session_index_jsonl"]["files_missing_or_unreadable"] == 1


def test_session_cap_and_bad_argument(tmp_path, capsys):
    base = tmp_path / "k" / "sessions" / "wd_a"
    for i in range(kimi_structure.MAX_SESSIONS + 3):
        (base / f"session_{i:04d}").mkdir(parents=True)
    code, doc, _ = _run(tmp_path / "k", capsys)
    assert code == 0
    assert doc["sessions"] == {
        "found": kimi_structure.MAX_SESSIONS + 3,
        "scanned": kimi_structure.MAX_SESSIONS,
        "truncated": True,
    }
    assert kimi_structure.main([str(tmp_path / "nope")]) == 2
    assert str(tmp_path) not in capsys.readouterr().out
