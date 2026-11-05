"""Shared MiniMax Code schema constants.

Split out of ``minimax_code.py`` so the ledger readers and the parser share
one definition of the runtime's tables. ``minimax_code`` re-exports every name.

This is not an extractor, so it stays out of ``event_extractors``: that
package is one module per provider, and each of those modules stamps a
``source_app``.
"""

from __future__ import annotations

# The provider the events are filed under. The MiniMax Code runtime bills
# the same upstream account as the MiniMax quota gauge, so the events land
# on the same card instead of splitting it.
PROVIDER_ID = "minimax"

# The runtime's internal plan-review sub-agent calls. They are real API
# requests against the user's plan quota, so they are included and tagged as
# a "Plan" subagent — the Sessions view already splits subagent turns out
# of the main-thread message count.
INTERNAL_TURN_PREFIX = "mavis-internal:plan-review:"
SUBAGENT_TYPE_PLAN = "Plan"

# The runtime's background-task sessions. It spawns them as *child* sessions:
# their own session id, a ``parent_session_id`` pointing at the session that
# spawned them, an ``agent_name`` naming the agent and a ``purpose`` naming
# the background task.
#
# Claude Code models the same work as a sidechain: the message carries the
# PARENT's session id plus an attribution agent (see the sidechain fixture in
# tests/fixtures/anthropic-sidechain-sample.jsonl). The rows here are filed
# under the parent session and tagged as subagent work, so the Sessions view
# splits them out of the parent's main thread exactly like a sidechain.
SUBAGENT_TYPE_TASK = "Task"

# The session columns the extractor reads when the store has them. They are
# optional on purpose: the runtime added them in a later migration than the
# columns in ``_REQUIRED_COLUMNS``, and a store without them must degrade to
# "no attribution" — never to "skipped source", which would drop every row.
_OPTIONAL_SESSION_COLUMNS: tuple[str, ...] = (
    "agent_name",
    "parent_session_id",
    "session_kind",
    "purpose",
)

# The ledger's own per-row agent name. It agrees with the session row's, and
# is the fallback when the session row has none. Optional for the same reason
# as the session columns above.
_OPTIONAL_USAGE_COLUMNS: tuple[str, ...] = ("agent_name",)

_TOKEN_USAGE_TABLE = "local_runtime_token_usage"
_MESSAGE_ROWS_TABLE = "local_runtime_message_rows"
_SESSIONS_TABLE = "local_runtime_sessions"

# The columns the extractor needs. The runtime owns its schema (≈34
# migrations and counting), so a rename must degrade to "skipped source"
# rather than raise into the sidecar loop.
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    _TOKEN_USAGE_TABLE: (
        "id",
        "session_id",
        "turn_id",
        "ts",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ),
    _MESSAGE_ROWS_TABLE: ("id", "session_id", "turn_id", "data_json"),
    _SESSIONS_TABLE: (
        "session_id",
        "workspace_dir",
        "extra_data_json",
        "history_relative_dir",
    ),
}

# Rows scanned per cycle. A first run over a long-lived store can see tens
# of thousands of requests; the cursor advances to the last row this cycle
# covered, so the rest is picked up by the next cycle instead of producing
# one enormous push.
_MAX_ROWS_PER_CYCLE = 5000

# How many cycles a row may be skipped before the extractor gives up on it.
#
# A skipped row (an unusable timestamp, a colliding event id, a payload the
# schema rejects) blocks the cursor: the cursor must not step over it, or the
# row's usage is lost for good. The row is retried on every cycle until this
# bound is reached; after that it is reported and abandoned, because a row
# that cannot be turned into a valid push will never become one.
_MAX_SKIP_ATTEMPTS = 3

# How many abandoned row ids the cursor file keeps per store. The list is a
# report for the operator — the rows are already gone from the ledger's
# perspective — so it is capped rather than allowed to grow forever.
_MAX_ABANDONED_RECORDS = 100

# The provider prefix the runtime writes into ``effectiveModel``. The
# pricing seed keys on the bare model id, so the prefix is stripped.
_PROVIDER_MODEL_PREFIX = "minimax/"
