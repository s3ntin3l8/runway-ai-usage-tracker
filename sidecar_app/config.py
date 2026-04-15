"""Re-export config helpers from scripts/sidecar.py and add write_template_config."""

import json
import pathlib
import sys

# scripts/ is not a package; use sys.path injection to import it
if getattr(sys, "frozen", False):
    _BASE = pathlib.Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    _BASE = pathlib.Path(__file__).parent.parent

sys.path.insert(0, str(_BASE / "scripts"))
import sidecar as _sidecar  # noqa: E402

load_config = _sidecar.load_config
get_sidecar_dir = _sidecar.get_sidecar_dir
get_log_path = _sidecar.get_log_path
setup_logging = _sidecar.setup_logging


def get_config_path() -> pathlib.Path:
    """Return the platform-specific path to the sidecar config file."""
    return get_sidecar_dir() / "config.json"


_TEMPLATE_CONFIG: dict = {
    "api_url": "http://localhost:8765",
    "api_key": "REPLACE_ME",
    "interval_seconds": 1800,
}


def write_template_config(config_path: pathlib.Path) -> None:
    """Write a minimal template config.json to *config_path*."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(_TEMPLATE_CONFIG, indent=2))
