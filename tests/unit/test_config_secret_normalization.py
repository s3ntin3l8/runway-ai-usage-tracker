"""Empty/blank/whitespace normalization for the security secrets.

Guards the empty-`ADMIN_API_KEY` auth bypass: a blank value must collapse to
None so it reads as "not configured" everywhere, never as a comparable key.
`_env_file=None` keeps these deterministic regardless of the repo `.env`.
"""

from app.core.config import Settings


def test_blank_admin_key_becomes_none():
    assert Settings(_env_file=None, ADMIN_API_KEY="").ADMIN_API_KEY is None
    assert Settings(_env_file=None, ADMIN_API_KEY="   ").ADMIN_API_KEY is None


def test_admin_key_is_stripped():
    assert Settings(_env_file=None, ADMIN_API_KEY="  k  ").ADMIN_API_KEY == "k"


def test_blank_encryption_key_becomes_none():
    assert Settings(_env_file=None, DB_ENCRYPTION_KEY="").DB_ENCRYPTION_KEY is None
    assert Settings(_env_file=None, DB_ENCRYPTION_KEY="  ").DB_ENCRYPTION_KEY is None


def test_blank_ingest_key_stays_empty_disabled():
    # "" is INGEST_API_KEY's "disabled" sentinel — keep it a string, just de-padded.
    assert Settings(_env_file=None, INGEST_API_KEY="   ").INGEST_API_KEY == ""


def test_ingest_key_is_stripped():
    raw = "  tok  "
    s = Settings(_env_file=None, **{"INGEST_API_KEY": raw})
    assert s.INGEST_API_KEY == raw.strip()


def test_unset_keys_keep_defaults():
    s = Settings(_env_file=None)
    assert s.ADMIN_API_KEY is None
    assert s.DB_ENCRYPTION_KEY is None
    assert s.INGEST_API_KEY == ""
