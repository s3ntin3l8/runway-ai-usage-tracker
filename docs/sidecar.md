# Universal Sidecar Collector

The **Runway Sidecar** is a lightweight, zero-dependency Python script designed to collect AI usage metrics directly from your host machine and push them to a Runway instance (e.g., running in Docker).

## 🚀 One-Liner Setup

Run the following command on your host machine to set up the sidecar as a recurring background task:

```bash
python3 scripts/sidecar.py --install
```

The script will interactively ask for your **Runway API URL** and **Ingestion API Key**.

## 🛠 Features

- **Zero Dependencies:** Uses only the Python Standard Library (`urllib`). No `pip install` required.
- **Multi-Provider Support:**
  - **Claude (Anthropic):** Automatically reads OAuth credentials from `~/.claude/.credentials.json`.
  - **GitHub Copilot:** Uses `GITHUB_TOKEN` from your environment.
  - **Gemini:** Scans local session logs in `~/.gemini/tmp/sessions`.
- **Cross-Platform:** Works on **macOS, Linux (Crontab)** and **Windows (Task Scheduler)**.
- **Auto-Installer:** Built-in task registration logic.

## 📖 Usage Options

### Manual Test (Dry Run)
Check what metrics are being collected without pushing them to the API:
```bash
python3 scripts/sidecar.py --dry-run
```

### Manual Push
Push metrics manually to a specific Runway instance:
```bash
python3 scripts/sidecar.py --api-url http://localhost:8765 --api-key <secret>
```

### Filtering Providers
Only collect metrics for a specific provider:
```bash
python3 scripts/sidecar.py --provider anthropic --dry-run
```

## 🐳 Docker Deployment Tip

If you are running Runway in Docker, the sidecar script is the best way to get host-side metrics (like Claude Code usage) into the container. 

1.  Ensure your Runway container is running and the port is mapped (e.g., `-p 8765:8765`).
2.  Run the sidecar on your **Host OS** using the `localhost` URL.
3.  The sidecar will reliably push metrics every 30 minutes into the running container's API.
