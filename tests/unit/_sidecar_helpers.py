"""Shared helpers and fixtures for sidecar unit tests."""

from __future__ import annotations

from pathlib import Path

from tests.unit._sidecar_credentials_helpers import isolate_host_credentials

__all__ = ["REPO_ROOT", "isolate_host_credentials"]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
