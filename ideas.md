Listed directory ai-usage-tracker
Viewed main.py:1-367
Browser task: "Researching cockpit-tools Auth Flow"
Read URL: https://github.com/jlcodes99/cockpit-tools
Viewed content.md:1-800
Searched web: "jlcodes99/cockpit-tools authentication oauth implementation details"

The comparison between `ai-usage-tracker` and `jlcodes99/cockpit-tools` reveals that while your current project is an excellent **passive monitor**, `cockpit-tools` is an **active bridge** that manages the entire lifecycle of multiple accounts.

### 🔄 Comparison of Auth Flows

| Feature | Current `ai-usage-tracker` | `cockpit-tools` (The Alternative) |
| :--- | :--- | :--- |
| **Source of Truth** | `.env` variables or existing local files. | Its own managed database (`~/.antigravity_cockpit`). |
| **Token Acquisition** | Manual (User must copy-paste tokens). | **Active OAuth**: Handles the login flow in-app. |
| **Account Limit** | Usually 1 per provider (hardcoded in `.env`). | **Multi-Account**: List of many accounts per provider. |
| **Workflow** | Reads state and displays it. | **Credential Injection**: Writes selected tokens back to the IDE's local files to "switch" users. |
| **Token Longevity** | Expires when the manual token expires. | **Auto-Refresh**: Background tasks refresh OAuth tokens. |

---

### 💡 Smarter Ways to "Take Over"

If you want to evolve `ai-usage-tracker`, here are the "smarter" patterns from `cockpit-tools` that would be most valuable:

#### 1. Credential Injection (Active Switching)
Currently, you *read* from `~/.gemini` or `~/.codex`. A "Cockpit-style" improvement would be to allow the user to select an account in your dashboard and have `main.py` **write** that token back to those files. This turns your tracker into a "Switcher."

#### 2. Local OAuth Helper
Instead of asking users to find hidden tokens, you can implement a small OAuth callback listener in FastAPI.
> [!TIP]
> You could add a "Login" button that opens the browser to the provider's OAuth page, captures the redirect on a local port, and saves the token to your `.env` or a local JSON database automatically.

#### 3. WebSocket Plugin Sync
`cockpit-tools` uses a local WebSocket (port `19528`) to talk to a browser extension. When a user logs into a web-based AI (like Claude or ChatGPT), the extension "sniffs" the token and pushes it to the local app. This is the "smartest" way to keep tokens fresh without manual work.

#### 4. Background Refresh Tasks
For providers like Google (Gemini CLI), tokens rotate frequently. You could implement a background `asyncio` task in `main.py` that checks for expiring tokens and uses the `refresh_token` to get a new one, keeping the dashboard (and the CLI) perpetually authenticated.

#### 5. Unified "Local Sync" Directory
Instead of scattering logic for `~/.claude`, `~/.gemini`, etc., you could adopt a pattern where you maintain a "Sync Folder" that maps your internal account database to the standard IDE locations.

### Recommendation
If you want to keep the tool lightweight, I suggest starting with **Multi-Account Support** (saving a list of tokens in a JSON file instead of just `.env`) and **Credential Injection** (the ability to write to the local files you currently only read).

Created ideas.md
Searched web: "jlcodes99/cockpit-tools source code structure quota fetching logic"

The specific way each tool gathers information differs fundamentally in its **source of truth** and **depth of integration**.

While `ai-usage-tracker` works like a "forensic tool" (looking at what's already on your disk), `cockpit-tools` works like a "remote agent" (talking directly to the provider's brain).

### 🛠 Comparison of Information Gathering

| Service | `ai-usage-tracker` (Scraping) | `cockpit-tools` (API-First) |
| :--- | :--- | :--- |
| **Claude** | **Local Logs**: Scans `~/.claude/projects` for `.jsonl` files and sums up tokens manually. Limits are hardcoded in `main.py`. | **Direct API**: Calls Anthropic's internal `/api/oauth/usage` endpoint. Gets dynamic limits and reset times directly from the server. |
| **Gemini** | **Local Sessions**: Reads `~/.gemini/tmp/sessions` to count prompt/completion tokens. | **Google API**: Uses OAuth credentials to query Google's management API for exact quota status. |
| **GitHub Copilot** | **API**: Uses `GITHUB_TOKEN` for `/rate_limit`. (Only core API, not copilot-specific). | **Copilot API**: Queries the specific Copilot billing/usage endpoints to distinguish between Inline and Chat suggestions. |
| **Codex** | **Local Cache**: Reads `~/.codex/sessions` for `used_percent`. | **Sync & Fetch**: Hydrates account metadata by calling Codex's `/account` and `/usage` APIs using the user's `auth.json`. |
| **OpenCode** | **SQLite**: Directly queries the `opencode.db` file for line changes. | **API**: Uses the OpenCode management endpoints for USD balance and hard limits. |
| **Windsurf / Trae** | **Not fully supported**. | **Local Injection**: Reads and *writes* to the platform's local storage (LevelDB or JSON) to sync account state. |

---

### 🧐 Why `cockpit-tools` is "Smarter"

1.  **Dynamic vs. Hardcoded**: Your current Claude collector assumes a `2,000,000` token limit. `cockpit-tools` fetches your "Plan" (Pro, Team, etc.) from the API and adjusts the limit automatically.
2.  **State Hydration**: When `cockpit-tools` imports a token, it immediately "hydrates" it—calling an official `/me` endpoint to get your username, avatar, and exact subscription tier.
3.  **The "Injection" Loophole**: Instead of just reading logs (which can be delayed), `cockpit-tools` manages the auth state. It can refresh a token *before* it expires, ensuring the gathering logic never fails due to an old session.
4.  **Strategy Pattern**: It uses a standardized code structure where each provider (Copilot, Cursor, etc.) implements a `getRemainingQuota()` method, making it trivial to add new ones.

### 💡 What we should adopt?

If you want to keep using your current "Scraping" approach (which is safer as it doesn't require as many permissions), you could still **adopt the API-first check** as a fallback.

> [!IMPORTANT]
> **The biggest win would be to move away from hardcoded limits.** Even if we continue reading local logs, we should try to find the "Plan" information in the local config files to set the usage progress bars accurately.