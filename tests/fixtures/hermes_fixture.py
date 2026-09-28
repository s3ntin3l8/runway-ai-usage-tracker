"""Helper to create a test Hermes SQLite database."""

import sqlite3


def make_hermes_db(path: str = ":memory:") -> sqlite3.Connection:
    """Create a Hermes-shaped SQLite DB with sample sessions and model usage."""
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            cwd TEXT,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0,
            profile_name TEXT,
            git_branch TEXT,
            git_repo_root TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_model_usage (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            billing_provider TEXT NOT NULL DEFAULT '',
            billing_base_url TEXT NOT NULL DEFAULT '',
            billing_mode TEXT NOT NULL DEFAULT '',
            task TEXT NOT NULL DEFAULT '',
            api_call_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            actual_cost_usd REAL NOT NULL DEFAULT 0,
            cost_status TEXT,
            cost_source TEXT,
            first_seen REAL,
            last_seen REAL,
            PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
        )
    """)

    # Seed sample session 1: Kimi Coding PR review session
    cur.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            cwd, git_branch, started_at, ended_at, input_tokens, output_tokens,
            estimated_cost_usd, actual_cost_usd
        ) VALUES (
            'api-sess-kimi-01', 'api_server', 'review-bot', 'kimi-for-coding',
            'kimi-coding', 'https://api.kimi.com/coding/v1/', '/home/bjoern/projects/runway',
            'feat/review-check', 1780000000.0, 1780000500.0, 50000, 2000, 0.0, 0.0
        )
    """)
    cur.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'api-sess-kimi-01', 'kimi-for-coding', 'kimi-coding',
            'https://api.kimi.com/coding/v1/', '', '', 10, 50000, 2000, 200000,
            0, 500, 0.0, 0.0, 'unknown', 'none', 1780000000.0, 1780000500.0
        )
    """)

    # Seed sample session 2: MiniMax with background review task
    cur.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            cwd, git_branch, started_at, ended_at, input_tokens, output_tokens,
            estimated_cost_usd, actual_cost_usd
        ) VALUES (
            'api-sess-minimax-02', 'api_server', 'review-bot', 'MiniMax-M3',
            'minimax-oauth', 'https://api.minimax.io/anthropic', '/home/bjoern/projects/app',
            'main', 1780001000.0, 1780001500.0, 80000, 4000, 0.05, 0.05
        )
    """)
    cur.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'api-sess-minimax-02', 'MiniMax-M3', 'minimax-oauth',
            'https://api.minimax.io/anthropic', '', 'background_review', 5, 80000, 4000, 150000,
            0, 0, 0.05, 0.05, 'final', 'provider_models_api', 1780001000.0, 1780001500.0
        )
    """)

    conn.commit()
    return conn
