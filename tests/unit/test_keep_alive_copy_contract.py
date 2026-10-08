"""Which logins keep-alive covers must read the same everywhere it is described."""

import pathlib
import re

from app.services.refresh_policy import KEEP_ALIVE_LABELS, KEEP_ALIVE_PROVIDERS

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_providers_are_derived_from_the_labels():
    assert set(KEEP_ALIVE_LABELS) == KEEP_ALIVE_PROVIDERS


def test_the_fleet_tooltip_names_exactly_the_covered_logins():
    ts = (ROOT / "webapp" / "src" / "lib" / "keepAlive.ts").read_text(encoding="utf-8")
    block = re.search(r"KEEP_ALIVE_LOGINS = \[(.*?)\] as const", ts, re.DOTALL)
    assert block, "KEEP_ALIVE_LOGINS not found in keepAlive.ts"
    names = re.findall(r"'([^']+)'", block.group(1))
    assert sorted(names) == sorted(KEEP_ALIVE_LABELS.values())


def test_the_fleet_per_login_list_maps_every_provider_id_to_its_label():
    ts = (ROOT / "webapp" / "src" / "lib" / "keepAlive.ts").read_text(encoding="utf-8")
    block = re.search(r"KEEP_ALIVE_PROVIDER_LABELS = \{(.*?)\} as const", ts, re.DOTALL)
    assert block, "KEEP_ALIVE_PROVIDER_LABELS not found in keepAlive.ts"
    pairs = dict(re.findall(r"(\w+): '([^']+)'", block.group(1)))
    assert pairs == KEEP_ALIVE_LABELS


def test_the_sidecar_help_text_names_every_covered_login():
    src = (ROOT / "scripts" / "sidecar.py").read_text(encoding="utf-8")
    start = src.index('"--keep-alive"')
    help_text = src[start : src.index("parser.add_argument(", start)]
    for label in KEEP_ALIVE_LABELS.values():
        # "Antigravity (agy)" -> "Antigravity"; "xAI (Grok)" -> "xAI"
        assert label.split(" (")[0] in help_text, f"--keep-alive help does not mention {label}"


def test_the_sidecar_settings_dialog_uses_the_shared_list_not_hardcoded_names():
    page = (ROOT / "webapp" / "src" / "features" / "fleet" / "SidecarSettingsDialog.tsx").read_text(
        encoding="utf-8"
    )
    assert "keepAliveLoginsText()" in page
    assert "agy and xAI" not in page
