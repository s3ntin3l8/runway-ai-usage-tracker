"""Bootstrap entry point for the Runway sidecar desktop app."""

import sys
import traceback

print("[DIAG] __main__ imports starting", flush=True)

from sidecar_app.config import get_config_path, load_config, write_template_config
from sidecar_app.daemon import TrayDaemon
from sidecar_app.tray import SidecarTray
from sidecar_app.updater import UpdateChecker

print("[DIAG] __main__ imports done", flush=True)

_FALLBACK_CONFIG: dict = {
    "api_url": "http://localhost:8765",
    "api_key": "",
    "interval_seconds": 1800,
}


def main() -> None:
    print("[DIAG] main() entered", flush=True)
    # 1. Find config path
    config_path = get_config_path()
    print(f"[DIAG] config_path={config_path}", flush=True)

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
    print("[DIAG] creating TrayDaemon", flush=True)
    daemon = TrayDaemon(config)
    print("[DIAG] creating SidecarTray", flush=True)
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
    print("[DIAG] calling tray.run()", flush=True)
    if needs_setup_notification:

        def notify_setup() -> None:
            if tray._icon is not None:
                tray._icon.notify(
                    "Edit config.json to connect to your Runway server, then restart.",
                    "Runway Sidecar — Setup Required",
                )

        tray.run(after_start=notify_setup)
    else:
        tray.run()

    # 8. Tray exited — stop background checker
    checker.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("[DIAG] UNHANDLED EXCEPTION in main():", flush=True)
        traceback.print_exc()
        input("Press Enter to exit...")  # keep window open
