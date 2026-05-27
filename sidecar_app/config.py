"""Re-export config helpers from scripts/sidecar.py and add write_template_config."""

import json
import os
import pathlib
import sys
import threading
from collections.abc import Callable

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
}


def write_template_config(config_path: pathlib.Path) -> None:
    """Write a minimal template config.json to *config_path*."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(_TEMPLATE_CONFIG, indent=2))


def watch_config(
    config_path: pathlib.Path,
    on_change: Callable[[dict], None],
    stop_event: threading.Event,
    poll_interval: float = 30.0,
) -> threading.Thread:
    """Start a background thread that polls *config_path* for changes every *poll_interval* seconds.

    Calls *on_change(new_config)* when the file's mtime changes and the config
    parses without error.  The thread exits when *stop_event* is set.

    Returns the started thread.
    """
    last_mtime: list[float] = [0.0]

    def _poll() -> None:
        while not stop_event.is_set():
            try:
                mtime = os.stat(config_path).st_mtime
                if mtime != last_mtime[0]:
                    if last_mtime[0] != 0.0:
                        # Only fire on actual changes (not the very first read)
                        try:
                            new_config = load_config(str(config_path))
                            on_change(new_config)
                        except Exception as e:
                            import logging

                            logging.warning(f"Config reload skipped — parse error: {e}")
                    last_mtime[0] = mtime
            except FileNotFoundError:
                pass  # file may not exist yet
            except Exception as e:
                import logging

                logging.debug(f"Config watcher error: {e}")
            stop_event.wait(timeout=poll_interval)

    t = threading.Thread(target=_poll, name="ConfigWatcher", daemon=True)
    t.start()
    return t
