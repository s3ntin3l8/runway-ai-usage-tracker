import logging
import os
from collections.abc import Iterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings

logger = logging.getLogger(__name__)

# Ensure data directory exists
db_dir = os.path.dirname(settings.DATABASE_PATH)
if not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create database directory {db_dir}: {e}")

SQLITE_CONNECT_ARGS = {
    # check_same_thread=False: required so FastAPI's worker threads can
    # share the connection pool.
    "check_same_thread": False,
    # SQLite is single-writer; busy_timeout makes concurrent writers wait for
    # the lock instead of failing immediately with "database is locked".
    "timeout": 30.0,
}


def configure_sqlite_engine(engine_: Engine) -> None:
    """Wire up the SQLAlchemy-recommended pattern for proper SQLite
    transaction semantics.

    Four things happen at connect time, in order:

    1. `isolation_level=None` puts the DBAPI connection in autocommit so
       SQLAlchemy controls BEGIN/COMMIT/ROLLBACK. Without this, Python's
       sqlite3 silently auto-commits on certain statements and SQLAlchemy's
       ROLLBACK has nothing to roll back — partial-batch failures leak.
    2. WAL journal mode and synchronous=NORMAL are applied per-connection
       before any transaction is open. WAL lets readers proceed alongside
       a writer; without it, every read serialises through the same lock
       as every write, and BEGIN times out under fleet contention.
    3. Read-performance pragmas (temp_store, cache_size, mmap_size) — see
       inline comments. Connection-scoped like the pragmas above (unlike
       journal_mode, SQLite doesn't persist these in the DB header), so they
       must be reapplied on every connect, not just once at startup.
    4. The "begin" event emits plain `BEGIN` (DEFERRED). Combined with WAL
       this is correct: writers serialise through SQLite's single writer
       lock with busy_timeout retry, readers use snapshots and never
       conflict. BEGIN IMMEDIATE would serialise reads as well — a 30s
       timeout is not enough margin for a healthy dashboard polling loop.
    """

    @event.listens_for(engine_, "connect")
    def _on_connect(dbapi_conn: Any, _conn_record: Any) -> None:
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        try:
            # `journal_mode=WAL` returns the journal mode actually in effect
            # ("memory" for :memory: DBs, "wal" for files). Either way it's
            # safe to set on every connect — it's a no-op once enabled.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # temp_store=MEMORY keeps SQLite's temp b-trees (built for
            # ORDER BY / GROUP BY / window-function work that can't be
            # satisfied by an index — the ranking and forecast aggregations
            # all do this) in RAM instead of spilling to a temp file on disk.
            cursor.execute("PRAGMA temp_store=MEMORY")
            # cache_size=-20000: ~20MB page cache (negative = KiB), up from
            # SQLite's ~2MB default. Cheap given this app's local-first,
            # single-instance deployment; keeps the hot working set (recent
            # usage_events/quota_snapshots pages) resident across requests.
            cursor.execute("PRAGMA cache_size=-20000")
            # mmap_size=268435456 (256MB): memory-map the DB file so reads
            # against pages outside the page cache avoid a read() syscall.
            # A no-op on platforms/builds without mmap support.
            cursor.execute("PRAGMA mmap_size=268435456")
        except Exception as e:
            logger.warning(f"Could not set SQLite concurrency pragmas: {e}")
        finally:
            cursor.close()

    @event.listens_for(engine_, "begin")
    def _emit_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")


engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=SQLITE_CONNECT_ARGS,
)
configure_sqlite_engine(engine)


def init_db() -> None:
    """Create database tables if they don't exist."""
    from app.models.db import (  # noqa: F401  ensures models are registered
        AuditLog,
        CredentialSource,
        CredentialTag,
        LatestUsage,
        LatestUsageContribution,
        PendingCredentialTag,
        ProviderAccountLabel,
        ProviderConfig,
        ProviderPricing,
        QuotaSnapshot,
        SidecarRegistry,
        SystemConfig,
        UsageEvent,
        UsagePeriodRollup,
        UsageWindow,
        WebhookConfig,
        WebhookCredentialAlert,
    )

    # Concurrency pragmas (WAL / synchronous=NORMAL) are applied per-connection
    # in the "connect" event listener — they must run before any BEGIN, which
    # rules out doing them through SQLAlchemy's normal execute path.

    SQLModel.metadata.create_all(engine)
    logger.info(f"Database initialized at {settings.DATABASE_PATH}")

    from app.services.accumulator import (
        backfill_latest_usage_contributions,
        evict_orphan_error_rows,
        prune_orphan_latest_usage_contributions,
    )

    with Session(engine) as session:
        prune_orphan_latest_usage_contributions(session)
        evict_orphan_error_rows(session)
        if session.exec(select(LatestUsageContribution.id)).first() is None:
            backfill_latest_usage_contributions(session)
        session.commit()

    # Add columns introduced after initial schema (SQLite create_all doesn't ALTER)
    with engine.connect() as conn:
        _add_columns_if_missing(conn)
        _add_indexes_if_missing(conn)
        _rebuild_usage_window_table_for_series_identity(conn)
        _rebuild_quota_snapshot_table_for_variant(conn)
        _rebuild_quota_snapshot_indexes(conn)
        _backfill_quota_snapshot_variant(conn)
        _migrate_webhook_uniqueness(conn)
        _migrate_credential_tag_scoping(conn)
        _drop_redundant_credential_tag_indexes(conn)
        _scrub_residual_stale_health(conn)

    from app.services.pricing_seed import seed_pricing_table

    with Session(engine) as session:
        inserted = seed_pricing_table(session)
        if inserted:
            logger.info(f"Seeded provider_pricing with {inserted} new rows")

    # Rewrite legacy non-canonical account_ids (e.g. mixed-case emails that
    # split one account into card + events-only twins). Idempotent.
    from app.services.account_canonicalization import canonicalize_stored_account_ids

    with Session(engine) as session:
        canonicalize_stored_account_ids(session)

    # Account-independent event identity: collapse cross-account duplicate
    # events (retag double counts), then add the (provider_id, event_id)
    # unique index. No-op once the index exists.
    from app.services.event_identity_migration import migrate_to_provider_event_identity

    with Session(engine) as session:
        migrate_to_provider_event_identity(session)


_DEFERRED_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, sql_type_with_default)
    ("sidecar_registry", "collection_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
    ("sidecar_registry", "self_update_capable", "BOOLEAN"),
    ("sidecar_registry", "pending_update", "BOOLEAN NOT NULL DEFAULT 0"),
    ("sidecar_registry", "identity_sources", "TEXT"),
    ("credential_sources", "credential_expires_at", "DATETIME"),
    ("credential_sources", "token_types_json", "TEXT"),
    ("credential_sources", "last_attempt_at", "DATETIME"),
    ("credential_sources", "last_success_at", "DATETIME"),
    ("credential_sources", "last_error", "TEXT"),
    ("pending_credential_tags", "verify_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_credential_tags", "next_verify_at", "DATETIME"),
    ("system_config", "user_timezone", "VARCHAR"),
    ("system_config", "sidecar_update_channel", "VARCHAR"),
    ("system_config", "sidecar_auto_update", "BOOLEAN"),
    ("system_config", "session_secret_encrypted", "VARCHAR"),
    # Structured audit attribution alongside the legacy `actor` string.
    ("audit_log", "actor_type", "VARCHAR"),
    ("audit_log", "actor_meta_json", "VARCHAR"),
    ("usage_events", "subagent_type", "VARCHAR"),
    ("usage_events", "attribution_source", "VARCHAR NOT NULL DEFAULT 'unknown'"),
    # Working-directory / project context + tool names (universal project linking).
    ("usage_events", "cwd", "VARCHAR"),
    ("usage_events", "project", "VARCHAR"),
    ("usage_events", "git_branch", "VARCHAR"),
    ("usage_events", "tools_json", "VARCHAR"),
    # Per-component USD cost (groundwork for cost-composition views; backfilled
    # from token counts × historical pricing by scripts/backfill_cache_costs.py).
    ("usage_events", "cost_input", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_events", "cost_output", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_events", "cost_cache_read", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_events", "cost_cache_create", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_events", "cost_reported_usd", "FLOAT"),
    ("usage_events", "cost_estimated_usd", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_period_rollup", "cost_input", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_period_rollup", "cost_output", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_period_rollup", "cost_cache_read", "FLOAT NOT NULL DEFAULT 0"),
    ("usage_period_rollup", "cost_cache_create", "FLOAT NOT NULL DEFAULT 0"),
    ("quota_snapshots", "variant", "TEXT NOT NULL DEFAULT ''"),
    ("usage_windows", "series_model_id", "TEXT NOT NULL DEFAULT ''"),
    ("usage_windows", "series_variant", "TEXT NOT NULL DEFAULT ''"),
    # oai-sc: OpenAI service-credential cookie required by chatgpt.com/api/auth/session
    ("provider_configs", "oai_sc_cookie_encrypted", "VARCHAR"),
    ("provider_configs", "opencode_workspace_id", "VARCHAR"),
    ("provider_configs", "billing_type", "VARCHAR NOT NULL DEFAULT 'unknown'"),
    # Archive: hide discontinued providers from the dashboard while preserving data.
    ("provider_configs", "archived", "BOOLEAN NOT NULL DEFAULT 0"),
    # Claude Code per-message dimensions previously discarded by the JSONL
    # parser (effort, fast-mode, service tier, entrypoint/version, cache TTL
    # split, web-tool counts) — see docs/collectors/claude.md.
    ("usage_events", "tokens_cache_create_1h", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_events", "tokens_cache_create_5m", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_events", "effort", "VARCHAR"),
    ("usage_events", "speed", "VARCHAR"),
    ("usage_events", "service_tier", "VARCHAR"),
    ("usage_events", "entrypoint", "VARCHAR"),
    ("usage_events", "app_version", "VARCHAR"),
    ("usage_events", "web_search_requests", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_events", "web_fetch_requests", "INTEGER NOT NULL DEFAULT 0"),
    ("provider_pricing", "cache_create_1h_per_mtok", "FLOAT NOT NULL DEFAULT 0"),
    # Per-account webhook scoping: NULL = applies to all accounts (legacy rows).
    ("webhook_configs", "account_id", "VARCHAR"),
    # Opt-in (default on) for credential-health (expired/invalid) alerts,
    # alongside the existing threshold alerts. See app.services.credential_alerts.
    ("webhook_configs", "credential_alerts", "BOOLEAN NOT NULL DEFAULT 1"),
    ("pending_credential_tags", "quota_preview_json", "TEXT"),
    ("pending_credential_tags", "quota_preview_observed_at", "DATETIME"),
    ("pending_credential_tags", "claimed_account_id", "VARCHAR"),
]


_DEFERRED_INDEXES: list[tuple[str, str, str]] = [
    # index_name, table, comma-separated columns
    # Note: ix_quota_snapshots_series_ts is handled by _rebuild_quota_snapshot_indexes
    # so it can be rebuilt with variant included on existing databases.
    ("ix_usage_events_project_ts", "usage_events", "project, ts"),
    ("ix_usage_events_kind_ts", "usage_events", "kind, ts"),
]


def _add_indexes_if_missing(conn: Any) -> None:
    """Idempotently CREATE INDEX IF NOT EXISTS for indexes that postdate
    initial schema creation. SQLModel.create_all() only adds indexes for
    fresh tables, so existing databases miss newer indexes declared in
    __table_args__.
    """
    from sqlalchemy import text

    for name, table, cols in _DEFERRED_INDEXES:
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
        conn.commit()


def _quota_snapshot_table_constraint_covers_variant(conn: Any) -> bool:
    """Whether quota_snapshots' table-level UNIQUE constraint includes variant.

    Checked structurally via PRAGMA rather than matching an exact DDL
    substring: a table-level ``UniqueConstraint`` always materializes as an
    ``origin='u'`` index (SQLite doesn't honor the constraint's own name for
    it — it's always ``sqlite_autoindex_*``), distinct from any *explicitly*
    ``CREATE INDEX``-ed index sharing the same name (``origin='c'``, e.g.
    ``_rebuild_quota_snapshot_indexes``'s own ``uq_quota_snapshots_identity``
    below). Returns ``True`` (nothing to do) if the table doesn't exist yet,
    or has no such constraint at all — either is create_all()'s job, not
    this migration's.
    """
    from sqlalchemy import text

    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quota_snapshots'")
    ).first()
    if row is None:
        return True  # table doesn't exist yet (created fresh by create_all right after)

    for index_row in conn.execute(text("PRAGMA index_list(quota_snapshots)")):
        # index_list columns: seq, name, unique, origin, partial
        if index_row[3] != "u":
            continue
        index_name = index_row[1]
        cols = {
            info_row[2] for info_row in conn.execute(text(f"PRAGMA index_info('{index_name}')"))
        }
        return "variant" in cols
    return True  # no UNIQUE constraint on the table at all — nothing to rebuild


def _rebuild_quota_snapshot_table_for_variant(conn: Any) -> None:
    """Rebuild quota_snapshots so its table-level identity constraint covers variant.

    ``uq_quota_snapshots_identity`` predates the ``variant`` column: on a
    long-lived database the constraint is still baked into CREATE TABLE as
    ``UNIQUE (provider_id, account_id, window_type, model_id, ts)`` (no
    ``variant``), backed by an un-droppable ``sqlite_autoindex_*`` — SQLite
    can't ALTER a table constraint in place, and dropping/recreating the
    *named* index of the same name in ``_rebuild_quota_snapshot_indexes``
    below only adds a second, wider index alongside it. The old 5-column
    autoindex still silently rejects a second variant sharing every other
    key at the same timestamp (e.g. two Antigravity quota gauges polled in
    the same minute) — the row is dropped rather than stored.

    The table is rebuilt with the model's current DDL (this bakes in the
    6-column constraint as a fresh, correctly-scoped autoindex), and rows
    are copied across. Idempotent: a fresh database's inline constraint
    already covers variant from create_all(), and an already-migrated
    database's does too, so both skip.
    """
    from sqlalchemy import text
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models.db import QuotaSnapshot

    if _quota_snapshot_table_constraint_covers_variant(conn):
        return  # fresh DB (final shape) or already migrated

    dialect = conn.engine.dialect
    create_sql = str(CreateTable(QuotaSnapshot.__table__).compile(dialect=dialect)).strip()  # type: ignore[attr-defined]
    create_sql = create_sql.replace(
        "CREATE TABLE quota_snapshots", "CREATE TABLE quota_snapshots_new", 1
    )
    conn.execute(text(create_sql))
    conn.execute(
        text(
            "INSERT INTO quota_snapshots_new "
            "(id, provider_id, account_id, window_type, variant, model_id, ts, pct_used, reset_at) "
            "SELECT id, provider_id, account_id, window_type, variant, model_id, ts, pct_used, reset_at "
            "FROM quota_snapshots"
        )
    )
    conn.execute(text("DROP TABLE quota_snapshots"))
    conn.execute(text("ALTER TABLE quota_snapshots_new RENAME TO quota_snapshots"))
    # DROP TABLE took every index the old table had (including the plain,
    # non-unique ones) down with it — recreate them from the model so
    # ix_quota_snapshots_lookup / _ts survive the rebuild. The rebuild below
    # (_rebuild_quota_snapshot_indexes) separately (re)creates the explicit
    # named uq_quota_snapshots_identity / ix_quota_snapshots_series_ts.
    for index in QuotaSnapshot.__table__.indexes:  # type: ignore[attr-defined]
        index_sql = str(CreateIndex(index).compile(dialect=dialect)).strip()
        conn.execute(text(index_sql))
    conn.commit()
    logger.info("Migrated: rebuilt quota_snapshots so uq_quota_snapshots_identity covers variant")


def _rebuild_usage_window_table_for_series_identity(conn: Any) -> None:
    """Rebuild usage_windows so uniqueness includes the quota-series identity.

    Older databases have a table-level unique constraint that SQLite cannot
    alter in place. The new columns distinguish quota-card model/pool identity
    from the existing event model/sidecar breakdown. Legacy rows copy with
    empty series identity and remain explicitly unscoped.

    This init_db migration helper owns its transaction boundary, including
    early-return commits, so callers can safely continue schema initialization.
    """
    from sqlalchemy import MetaData, Table, insert, literal_column, select, text
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models.db import UsageWindow

    migration_id = "usage_windows_series_identity_v1"
    conn.execute(
        text("CREATE TABLE IF NOT EXISTS runway_schema_migrations (migration_id TEXT PRIMARY KEY)")
    )
    applied = conn.execute(
        text("SELECT 1 FROM runway_schema_migrations WHERE migration_id = :migration_id"),
        {"migration_id": migration_id},
    ).first()
    if applied:
        conn.commit()
        return

    required = (
        "provider_id",
        "account_id",
        "window_type",
        "window_end",
        "series_model_id",
        "series_variant",
        "model_id",
        "sidecar_id",
    )
    for row in conn.execute(text("PRAGMA index_list(usage_windows)")):
        if not row[2]:
            continue
        index_name = row[1].replace("'", "''")
        index_cols = tuple(
            info[2] for info in conn.execute(text(f"PRAGMA index_info('{index_name}')"))
        )
        if index_cols == required:
            # Fresh databases already have the current model constraint. Record
            # this migration so future schema edits cannot trigger a rebuild.
            conn.execute(
                text("INSERT INTO runway_schema_migrations (migration_id) VALUES (:migration_id)"),
                {"migration_id": migration_id},
            )
            conn.commit()
            return

    dialect = conn.engine.dialect
    create_sql = str(CreateTable(UsageWindow.__table__).compile(dialect=dialect)).strip()  # type: ignore[attr-defined]
    create_sql = create_sql.replace(
        "CREATE TABLE usage_windows", "CREATE TABLE usage_windows_new", 1
    )
    conn.execute(text(create_sql))
    columns = [column.name for column in UsageWindow.__table__.columns]  # type: ignore[attr-defined]
    source = Table("usage_windows", MetaData(), autoload_with=conn)
    target = Table("usage_windows_new", MetaData(), autoload_with=conn)
    select_columns = [
        source.c[name] if name in source.c else literal_column("''").label(name) for name in columns
    ]
    conn.execute(insert(target).from_select(columns, select(*select_columns)))
    conn.execute(text("DROP TABLE usage_windows"))
    conn.execute(text("ALTER TABLE usage_windows_new RENAME TO usage_windows"))
    for index in UsageWindow.__table__.indexes:  # type: ignore[attr-defined]
        conn.execute(text(str(CreateIndex(index).compile(dialect=dialect)).strip()))
    conn.execute(
        text("INSERT INTO runway_schema_migrations (migration_id) VALUES (:migration_id)"),
        {"migration_id": migration_id},
    )
    conn.commit()
    logger.info("Migrated: rebuilt usage_windows so uniqueness covers quota-series identity")


def _rebuild_quota_snapshot_indexes(conn: Any) -> None:
    """Rebuild quota_snapshot indexes to include the variant column.

    The unique constraint and series_ts index both need variant in their
    key. On existing databases the column was just added by _add_columns_if_missing;
    the old indexes must be dropped and recreated with the new column list.
    On fresh databases create_all() builds the correct indexes from __table_args__
    so PRAGMA index_info won't find variant missing and this is a no-op.
    """
    from sqlalchemy import text

    # Check that variant column exists before touching indexes.
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(quota_snapshots)"))}
    if "variant" not in cols:
        return

    _QUOTA_SNAPSHOT_INDEXES = [
        (
            "uq_quota_snapshots_identity",
            "CREATE UNIQUE INDEX uq_quota_snapshots_identity ON quota_snapshots "
            "(provider_id, account_id, window_type, variant, model_id, ts)",
        ),
        (
            "ix_quota_snapshots_series_ts",
            "CREATE INDEX ix_quota_snapshots_series_ts ON quota_snapshots "
            "(provider_id, account_id, window_type, variant, model_id, ts)",
        ),
    ]
    for index_name, create_sql in _QUOTA_SNAPSHOT_INDEXES:
        # Check whether the existing index already covers variant.
        index_cols = [row[2] for row in conn.execute(text(f"PRAGMA index_info('{index_name}')"))]
        if "variant" in index_cols:
            continue
        conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        conn.execute(text(create_sql))
        conn.commit()
        logger.info("Migrated: rebuilt %s to include variant", index_name)


def _backfill_quota_snapshot_variant(conn: Any) -> None:
    """Rewrite quota_snapshots rows written with variant='default' to variant=''.

    The accumulator previously stored the absent-variant sentinel as "default"
    while the forecast read path filters for "". This one-time UPDATE aligns
    historical rows with the column default and the read side expectation.
    OR IGNORE handles the (extremely unlikely) duplicate on the unique key.
    """
    from sqlalchemy import text

    result = conn.execute(
        text("UPDATE OR IGNORE quota_snapshots SET variant = '' WHERE variant = 'default'")
    )
    conn.commit()
    if result.rowcount:
        logger.info(
            "Migrated: backfilled %d quota_snapshots rows (variant '' <- 'default')",
            result.rowcount,
        )


def _add_columns_if_missing(conn: Any) -> None:
    """Idempotently ALTER TABLE ... ADD COLUMN for fields that postdate
    initial schema creation. SQLModel.create_all() only creates new tables
    on existing SQLite databases — it never adds new columns.

    Only the "duplicate column" race is swallowed silently; any other
    failure is re-raised so genuine schema drift is loud.
    """
    import sqlalchemy.exc
    from sqlalchemy import text

    for table, column, sql_type in _DEFERRED_COLUMNS:
        cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        if column in cols:
            continue
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            conn.commit()
            logger.info(f"Migrated: added {table}.{column}")
        except sqlalchemy.exc.OperationalError as e:
            if "duplicate column" in str(e).lower():
                continue
            raise


def _migrate_webhook_uniqueness(conn: Any) -> None:
    """Enforce (provider_id, account_id, url) uniqueness on webhook_configs.

    On fresh databases create_all() builds the unique constraint from
    __table_args__ (backed by a sqlite_autoindex_* index). On existing
    databases the constraint predates account_id, so the column is added by
    _add_columns_if_missing first and the unique index is created here.

    Note: SQLite treats NULLs as distinct in unique indexes, so pre-#274
    rows (all account_id=NULL after the column add) can never make
    CREATE UNIQUE INDEX fail — no pre-delete is needed or desirable
    (deleting would silently drop legitimate configs). The index only
    hardens rows with a concrete account_id; duplicates of the
    "all accounts" (NULL) form are rejected by the API layer instead.
    """
    from sqlalchemy import text

    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(webhook_configs)"))}
    if "account_id" not in cols:
        return

    # Already enforced? Fresh DBs carry the __table_args__ constraint under a
    # sqlite_autoindex_* name; migrated DBs carry uq_webhook_provider_account_url.
    needed = {"provider_id", "account_id", "url"}
    for row in conn.execute(text("PRAGMA index_list(webhook_configs)")):
        index_name, unique = row[1], row[2]
        if not unique:
            continue
        index_cols = {r[2] for r in conn.execute(text(f"PRAGMA index_info('{index_name}')"))}
        if needed <= index_cols:
            return

    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_provider_account_url "
            "ON webhook_configs (provider_id, account_id, url)"
        )
    )
    conn.commit()
    logger.info("Migrated: created unique index uq_webhook_provider_account_url")


def get_session() -> Iterator[Session]:
    """FastAPI dependency for DB session."""
    with Session(engine) as session:
        yield session


def _migrate_credential_tag_scoping(conn: Any) -> None:
    """Rebuild credential_tags for per-sidecar scoping (#319).

    Pre-#319 tables carry a table-level UNIQUE on
    ``(provider_id, credential_origin)`` (``uq_credential_tag_identity``)
    baked into CREATE TABLE — SQLite can't drop a table constraint in
    place, so the table is rebuilt with the post-#319 shape: a nullable
    ``sidecar_id`` column plus two partial unique indexes (the model's
    ``__table_args__``). NULLs are distinct in SQLite unique indexes, so
    the split is what keeps one deployment-wide row per origin while
    still allowing one scoped row per sidecar for the same origin.

    Existing rows are copied with ``sidecar_id = NULL`` — deployment-
    wide, matching their pre-migration semantics. Idempotent: fresh
    databases are created by ``create_all()`` in the final shape (no
    ``uq_credential_tag_identity`` in the table DDL) and already-migrated
    tables don't carry the legacy constraint either, so both skip.
    """
    from sqlalchemy import text
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models.db import CredentialTag

    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='credential_tags'")
    ).first()
    if row is None or row[0] is None or "uq_credential_tag_identity" not in row[0]:
        return  # fresh DB (final shape) or already migrated

    # Compile the model's CREATE TABLE so the new shape stays in lockstep
    # with __table_args__ / column definitions (no hand-maintained DDL).
    dialect = conn.engine.dialect
    create_sql = str(CreateTable(CredentialTag.__table__).compile(dialect=dialect)).strip()  # type: ignore[attr-defined]
    create_sql = create_sql.replace(
        "CREATE TABLE credential_tags", "CREATE TABLE credential_tags_new", 1
    )
    conn.execute(text(create_sql))
    conn.execute(
        text(
            "INSERT INTO credential_tags_new "
            "(id, provider_id, credential_origin, account_id, sidecar_id, set_by, set_at) "
            "SELECT id, provider_id, credential_origin, account_id, NULL, set_by, set_at "
            "FROM credential_tags"
        )
    )
    conn.execute(text("DROP TABLE credential_tags"))
    conn.execute(text("ALTER TABLE credential_tags_new RENAME TO credential_tags"))
    # Indexes (including the two partial unique ones) travel with the model.
    for index in CredentialTag.__table__.indexes:  # type: ignore[attr-defined]
        index_sql = str(CreateIndex(index).compile(dialect=dialect)).strip()
        conn.execute(text(index_sql))
    conn.commit()
    logger.info("Migrated: rebuilt credential_tags for per-sidecar scoping (#319)")


# Auto-generated ``Field(index=True)`` indexes that duplicated the explicit
# ``__table_args__`` indexes on the credential-tag tables (#322 review).
_REDUNDANT_CREDENTIAL_TAG_INDEXES = (
    "ix_credential_tags_provider_id",
    "ix_credential_tags_sidecar_id",
    "ix_pending_credential_tags_sidecar_id",
)


def _drop_redundant_credential_tag_indexes(conn: Any) -> None:
    """Drop duplicate single-column indexes left by earlier model versions.

    Idempotent (``DROP INDEX IF EXISTS``); fresh databases never create them.
    """
    from sqlalchemy import text

    existing = {
        row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
    }
    dropped = [name for name in _REDUNDANT_CREDENTIAL_TAG_INDEXES if name in existing]
    for name in dropped:
        conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
    if dropped:
        conn.commit()
        logger.info("Dropped redundant credential-tag indexes: %s", ", ".join(dropped))


def _scrub_residual_stale_health(conn: Any) -> None:
    """Mark residual pre-#293 collection-failure cards as `stale` and reconcile health.

    Before PR #293, collectors used health="critical" as a stale marker and never
    set `stale`. Rows that stopped being rewritten (collection failed → error card
    suppressed by upsert) keep that residual health forever — 0% cards land in the
    at-risk rail with no stale dimming. Detection matches the Collection-failing
    detail prefix (the legacy marker for pre-#293 rows) or an already-set
    `collection_failing` flag; matching rows are stamped `stale` +
    `collection_failing` and their residual health is reconciled. Idempotent: a
    fully-stamped row whose health already matches the percentage is left
    untouched (changed=False → no write).
    """
    import json as _json

    from sqlalchemy import text as _text

    from app.core.utils import HealthCalculator

    rows = conn.execute(_text("SELECT id, card_json FROM latest_usage")).fetchall()
    scrubbed = 0
    for row_id, card_json in rows:
        if not card_json:
            continue
        try:
            card = _json.loads(card_json)
        except (_json.JSONDecodeError, TypeError):
            continue
        if not isinstance(card, dict):
            continue
        detail = card.get("detail") or ""
        if "Collection failing" not in detail and card.get("collection_failing") is not True:
            continue
        changed = False
        if card.get("stale") is not True:
            card["stale"] = True
            changed = True
        if card.get("collection_failing") is not True:
            card["collection_failing"] = True
            changed = True
        if HealthCalculator.reconcile_residual_health(card):
            changed = True
        if not changed:
            continue
        conn.execute(
            _text("UPDATE latest_usage SET card_json = :card_json WHERE id = :id"),
            {"card_json": _json.dumps(card), "id": row_id},
        )
        scrubbed += 1
    if scrubbed:
        conn.commit()
        logger.info(f"Scrubbed residual stale health on {scrubbed} latest_usage card(s)")
