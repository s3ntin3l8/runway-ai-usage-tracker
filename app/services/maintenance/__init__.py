"""Shared repair/maintenance logic used by both the Data Health checks
(``app/services/data_health/``) and the standalone `scripts/*.py` CLIs.

``scripts/`` entry points are thin argparse wrappers over these functions —
the logic lives here once so an in-app fix and a host-run script can never
drift apart. See CLAUDE.md's Data Model section for the module map.
"""
