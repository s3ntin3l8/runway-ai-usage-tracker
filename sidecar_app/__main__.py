"""Bootstrap entry point for the Runway sidecar desktop app."""

from sidecar_app.config import get_config_path, load_config, write_template_config
from sidecar_app.daemon import TrayDaemon
from sidecar_app.tray import SidecarTray

_FALLBACK_CONFIG: dict = {
    "api_url": "http://localhost:8765",
    "api_key": "",
    "interval_seconds": 1800,
}


def main() -> None:
    # 1. Find config path
    config_path = get_config_path()

    # 2. Load config (handle missing without calling sys.exit)
    if not config_path.exists():
        write_template_config(config_path)
        config = dict(_FALLBACK_CONFIG)
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

    # 5. Start daemon only when credentials are present
    api_key = config.get("api_key", "")
    if api_key and api_key != "REPLACE_ME":
        daemon.start()

    # 6. Run tray — blocks main thread
    tray.run()


if __name__ == "__main__":
    main()
