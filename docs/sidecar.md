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
- **Multi-Provider Support:** Collects from 8 different AI providers
- **Cross-Platform:** Works on **macOS, Linux (Crontab)** and **Windows (Task Scheduler)**.
- **Auto-Installer:** Built-in task registration logic.

### Supported Providers

| Provider | Data Source | Required Environment |
|----------|-------------|---------------------|
| **Claude (Anthropic)** | OAuth API | `CLAUDE_CODE_OAUTH_TOKEN` or `~/.claude/.credentials.json` |
| **GitHub Copilot** | GitHub API | `GITHUB_TOKEN` |
| **Gemini** | OAuth API + Local logs | `GEMINI_OAUTH_CLIENT_ID/SECRET` or `~/.gemini/oauth_creds.json` |
| **ChatGPT** | Web API + Local logs | `CHATGPT_OAUTH_TOKEN` or `~/.codex/auth.json` |
| **OpenCode** | Local SQLite DB | `~/.local/share/opencode/opencode.db` |
| **zAI (GLM)** | Balance API | `ZAI_API_KEY` |
| **Kimi Code** | Balance API | `KIMI_API_KEY` |
| **Antigravity** | Local JSON file | `~/.antigravity/state/quota.json` |

## 📖 Usage Options

### Manual Test (Dry Run)

Check what metrics are being collected without pushing them to the API:

```bash
python3 scripts/sidecar.py --dry-run
```

Test a specific provider:
```bash
python3 scripts/sidecar.py --provider anthropic --dry-run
python3 scripts/sidecar.py --provider zai --dry-run
python3 scripts/sidecar.py --provider kimi_code --dry-run
```

### Manual Push

Push metrics manually to a specific Runway instance:

```bash
python3 scripts/sidecar.py --api-url http://localhost:8765 --api-key <secret>
```

### Filtering Providers

Only collect metrics for a specific provider:

```bash
# Claude (Anthropic)
python3 scripts/sidecar.py --provider anthropic --dry-run

# GitHub Copilot
python3 scripts/sidecar.py --provider github --dry-run

# Google Gemini
python3 scripts/sidecar.py --provider gemini --dry-run

# ChatGPT Codex
python3 scripts/sidecar.py --provider chatgpt --dry-run

# OpenCode
python3 scripts/sidecar.py --provider opencode --dry-run

# zAI (Zhipu AI)
python3 scripts/sidecar.py --provider zai --dry-run

# Kimi Code (Moonshot)
python3 scripts/sidecar.py --provider kimi_code --dry-run

# Antigravity IDE
python3 scripts/sidecar.py --provider antigravity --dry-run

# All providers (default)
python3 scripts/sidecar.py --provider all --dry-run
```

## 🐳 Deployment Modes

### Standalone (Single Machine)

When Runway runs on the same machine as your coding tools, you typically don't need a sidecar. However, you can still use it to:
- Test collection without running the full app
- Collect metrics from isolated environments

```bash
python3 scripts/sidecar.py --dry-run
```

### Multi-Host (Main PC + Laptop)

**Main PC** (runs full Runway app):
```bash
python3 -m app.main
```

**Laptop** (sidecar only):
```bash
python3 scripts/sidecar.py \
  --api-url http://main-pc:8765 \
  --api-key sidecar-default-secret
```

The main PC combines its own local data with data from the laptop's sidecar.

### Server/Docker (Containerized)

**Server** (Docker - no local file access):
```bash
docker run -p 8765:8765 \
  -e INGEST_API_KEY=your-secret-key \
  runway
```

**Each Workstation** (sidecar required):
```bash
python3 scripts/sidecar.py \
  --api-url http://server:8765 \
  --api-key your-secret-key
```

The server aggregates data from ALL workstation sidecars. Heavy lifting (API calls, aggregation) happens server-side.

## ⚙️ Configuration

### Environment Variables

The sidecar reads the following environment variables:

```bash
# GitHub Copilot
export GITHUB_TOKEN="github_pat_..."

# zAI (GLM)
export ZAI_API_KEY="sk-..."

# Kimi Code (Moonshot)
export KIMI_API_KEY="sk-proj-..."

# Claude (optional, will try keychain/file first)
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-..."

# ChatGPT (optional, will try auth file first)
export CHATGPT_OAUTH_TOKEN="..."
```

### Platform-Specific Notes

#### macOS
- Supports Keychain token extraction for Claude
- Uses `crontab` for scheduling
- Chrome cookies accessible for OpenCode/ChatGPT

#### Linux
- Uses `crontab` for scheduling
- Chrome cookies accessible (if using standard paths)

#### Windows
- Uses Task Scheduler for background tasks
- Chrome cookies accessible
- Run with `python` (not `python3`) in Task Scheduler

## 🔧 Troubleshooting

### Sidecar not collecting data

**Check provider-specific setup:**
```bash
# Test specific provider
python3 scripts/sidecar.py --provider anthropic --dry-run

# Check environment variables
env | grep -E "(GITHUB|ZAI|KIMI|CLAUDE)"
```

### Push failures

**Verify API connectivity:**
```bash
curl -X POST http://localhost:8765/api/ingest \
  -H "Content-Type: application/json" \
  -d '{"provider":"test","api_key":"your-key","metrics":[]}'
```

### Installation issues

**Check existing cron tasks:**
```bash
crontab -l | grep sidecar
```

**Check Task Scheduler (Windows):**
```powershell
schtasks /query /tn "RunwaySidecar"
```

## 📝 Provider-Specific Setup

### Claude (Anthropic)

The sidecar tries these authentication methods in order:
1. `CLAUDE_CODE_OAUTH_TOKEN` environment variable
2. `~/.claude/.credentials.json` file
3. macOS Keychain (on macOS only)

### OpenCode

Requires the local SQLite database:
- **Linux/macOS**: `~/.local/share/opencode/opencode.db`
- **Windows**: `%LOCALAPPDATA%\opencode\opencode.db`

Or extract Chrome session cookie if logged into opencode.ai.

### Antigravity

Reads from the IDE's quota export file:
- **All platforms**: `~/.antigravity/state/quota.json`

Ensure Antigravity IDE is running and has written the quota file.

---

*See individual collector docs in `docs/collectors/` for more details.*

*Last updated: 2026-04-07*
