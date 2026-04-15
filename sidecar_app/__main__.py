"""Bootstrap entry point for the Runway sidecar desktop app."""

import traceback

from sidecar_app.config import get_config_path, load_config, setup_logging, write_template_config
from sidecar_app.daemon import TrayDaemon
from sidecar_app.tray import SidecarTray
from sidecar_app.updater import UpdateChecker

_FALLBACK_CONFIG: dict = {
    "api_url": "http://localhost:8765",
    "api_key": "",
    "interval_seconds": 1800,
}


def main() -> None:
    # 0. Enable logging to file
    setup_logging(log_level="INFO", file_enabled=True)

    # 1. Find config path
    config_path = get_config_path()

    # 2. Load config (handle missing without calling sys.exit)
    needs_setup_notification = False
    if not config_path.exists():
        write_template_config(config_path)
        config = dict(_FALLBACK_CONFIG)
        needs_setup_notification = True
    else:
        try:
            config = load_config(str(config_path))
        except SystemExit:
            # load_config calls sys.exit on validation errors; treat as broken config
            config = dict(_FALLBACK_CONFIG)
        except Exception:
            config = dict(_FALLBACK_CONFIG)

    # 3. Create daemon + tray
    daemon = TrayDaemon(config)
    tray = SidecarTray(daemon, config, config_path)

    # 4. Wire status change callback
    daemon.on_status_change = tray._update_icon

    # 5. Wire update checker
    def on_update_available(current: str, latest: str) -> None:
        tray.set_update_available(True)

    checker = UpdateChecker(on_update_available=on_update_available)
    checker.start()

    # 6. Start daemon only when credentials are present
    api_key = config.get("api_key", "")
    if api_key and api_key != "REPLACE_ME":
        daemon.start()

    # 7. Run tray — blocks main thread
    if needs_setup_notification:

        def notify_setup() -> None:
            if tray._icon is not None:
                try:
                    tray._icon.notify(
                        "Edit config.json to connect to your Runway server, then restart.",
                        "Runway Sidecar — Setup Required",
                    )
                except Exception:
                    pass  # notifications not supported on all platforms

        tray.run(after_start=notify_setup)
    else:
        tray.run()

    # 8. Tray exited — stop background checker
    checker.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
