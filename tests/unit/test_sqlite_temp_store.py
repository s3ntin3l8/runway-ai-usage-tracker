"""Temporary-store configuration applies to every new SQLite connection."""

import pytest
from pydantic import ValidationError
from sqlmodel import create_engine

from app.core.config import Settings, settings
from app.core.db import configure_sqlite_engine


@pytest.mark.parametrize(("mode", "value"), [("MEMORY", 2), ("FILE", 1)])
def test_temp_store_applied_on_reconnect(tmp_path, monkeypatch, mode, value):
    monkeypatch.setattr(settings, "SQLITE_TEMP_STORE", mode)
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}")
    configure_sqlite_engine(engine)
    for _ in range(2):
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA temp_store").scalar_one() == value
            assert conn.exec_driver_sql("PRAGMA cache_size").scalar_one() == -20000
            assert conn.exec_driver_sql("PRAGMA mmap_size").scalar_one() == 268435456
        engine.dispose()


def test_temp_store_rejects_invalid_values():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SQLITE_TEMP_STORE="invalid")
