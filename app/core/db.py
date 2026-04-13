from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings
import os
import logging

logger = logging.getLogger(__name__)

# Ensure data directory exists
db_dir = os.path.dirname(settings.DATABASE_PATH)
if not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create database directory {db_dir}: {e}")

engine = create_engine(
    settings.DATABASE_URL, 
    echo=False,
    connect_args={"check_same_thread": False} # Needed for SQLite + FastAPI
)


def init_db():
    """Create database tables if they don't exist."""
    # Import all models here so they are registered with SQLModel.metadata
    from app.models.db import UsageSnapshot, SidecarRegistry  # noqa: F401
    
    try:
        SQLModel.metadata.create_all(engine)
        logger.info(f"Database initialized at {settings.DATABASE_PATH}")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")


def get_session():
    """FastAPI dependency for DB session."""
    with Session(engine) as session:
        yield session
