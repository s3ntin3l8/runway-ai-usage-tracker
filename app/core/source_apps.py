"""Canonical ``source_app`` values — the client app that produced an event.

``provider_id`` names the *upstream* provider a request was billed to, and
several client apps share one provider: MiniMax Code, OpenCode and a plain API
script can all spend the same MiniMax plan. ``source_app`` names the *client*
that made the call, so a canonical-provider retag (see
``scripts/sidecar_pkg/canonical_providers.py``) no longer erases where the
traffic came from.

Free-form on the wire: a new client app must be able to report usage before
Runway ships a release that knows its name. The constants below are the
canonical values the bundled sidecar extractors report; any other value is
stored and grouped like any other.
"""

from __future__ import annotations

# Client apps the bundled sidecar extractors report.
ANTIGRAVITY = "antigravity"
CLAUDE_CODE = "claude_code"
CODEX = "codex"
GEMINI_CLI = "gemini_cli"
HERMES = "hermes"
MINIMAX_CODE = "minimax_code"
OPENCODE = "opencode"
XAI_CLI = "xai_cli"

KNOWN_SOURCE_APPS: frozenset[str] = frozenset(
    {
        ANTIGRAVITY,
        CLAUDE_CODE,
        CODEX,
        GEMINI_CLI,
        HERMES,
        MINIMAX_CODE,
        OPENCODE,
        XAI_CLI,
    }
)
