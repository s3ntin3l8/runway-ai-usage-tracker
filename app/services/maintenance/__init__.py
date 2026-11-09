"""Shared repair/maintenance logic used by both the Data Health checks
(``app/services/data_health/``) and the standalone `scripts/*.py` CLIs.

Host-run ``scripts/`` entry points wrap these functions where one exists —
the logic lives here once so an in-app fix and a host-run script cannot drift
apart (a few scripts are standalone). See the "Repairs and Data health" part
of docs/architecture.md's Data model section.
"""
