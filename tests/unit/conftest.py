# Phase 1 schema reset: these test files reference deleted DB models
# (UsageSnapshot, UsageSnapshotModel, CumulativeUsage) and will be
# rewritten in later phases. Excluded from collection until then.
collect_ignore = [
    "test_accumulator.py",
    "test_compaction.py",
    "test_db_token_fields.py",
    "test_history_deltas.py",
    "test_history_helpers.py",
    "test_new_db_schemas.py",
    "test_poller_tokens.py",
]
