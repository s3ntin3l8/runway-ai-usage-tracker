from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class ClaudeCloudVerification(SQLModel, table=True):  # type: ignore[call-arg]
    """Single operator-owned encrypted verification checkpoint; no credentials."""

    __tablename__ = "claude_cloud_verification"
    id: int = Field(default=1, primary_key=True)
    job_handle: str
    revision: int = 0
    state_encrypted: str
    lease_owner: str | None = None
    lease_expires: float = 0


class ClaudeCloudVerificationArchive(SQLModel, table=True):  # type: ignore[call-arg]
    """Original encrypted terminal envelopes retained by explicit admin action."""

    __tablename__ = "claude_cloud_verification_archives"
    job_handle: str = Field(primary_key=True)
    revision: int
    state_encrypted: str
    archived_at: float


class ClaudeCloudSessionCheckpoint(SQLModel, table=True):  # type: ignore[call-arg]
    """Where the history sync of one Claude cloud-runner session stands.

    The session is held as a one-way digest. ``committed_sequence`` and the cursor of the page that
    holds it mark the last *closed turn*: the history is read again from there, so the open tail
    after the last result is simply re-read. The cursor is opaque to us and is stored encrypted.
    ``reducer_state_json`` is the serialisable state of ``claude_cloud_events``.
    """

    __tablename__ = "claude_cloud_session_checkpoint"
    __table_args__ = (
        UniqueConstraint("account_id", "session_digest", name="uq_claude_cloud_checkpoint"),
    )
    id: int | None = Field(default=None, primary_key=True)
    account_id: str = Field(index=True)
    session_digest: str
    last_event_at: str | None = None
    committed_sequence: int = 0
    committed_cursor_encrypted: str | None = None
    reducer_state_json: str = "{}"
    open_tail: bool = False
    stalls: int = 0
    turns: int = 0
    message_events: int = 0
    remainder_events: int = 0
    last_crosscheck: str | None = None
    last_seen: float = 0.0


class ClaudeCloudSessionUsage(SQLModel, table=True):  # type: ignore[call-arg]
    """Last usage block observed for one Claude cloud-runner session.

    The collector writes only the increase since this row as ``usage_events``. The session
    is held as a one-way digest; counters are ``None`` until the API has reported them.
    """

    __tablename__ = "claude_cloud_session_usage"
    __table_args__ = (
        UniqueConstraint("account_id", "session_digest", name="uq_claude_cloud_session"),
    )
    id: int | None = Field(default=None, primary_key=True)
    account_id: str = Field(index=True)
    session_digest: str
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_create: int | None = None
    shrunk_streak: int = 0
    model_id: str | None = None
    # One per event written for this session; part of the event's dedup identity.
    revision: int = 0
    regressions: int = 0
    last_seen: float = 0


class ClaudeCloudCollectorState(SQLModel, table=True):  # type: ignore[call-arg]
    """Per-account progress and outcome of the Claude cloud-runner collector."""

    __tablename__ = "claude_cloud_collector_state"
    account_id: str = Field(primary_key=True)
    # Newest session ``updated_at`` seen by a sweep that reached its end or stopped early.
    high_water: str | None = None
    cycles: int = 0
    last_cycle_at: float = 0
    last_outcome: str = "never_run"
    last_events: int = 0
    last_regressions: int = 0
    # Cycles in a row that failed (busy ones change nothing); 0 after ok/partial/truncated.
    consecutive_failures: int = 0
    # 1 while the last cycle left sessions unread.
    backlog: int = 0
