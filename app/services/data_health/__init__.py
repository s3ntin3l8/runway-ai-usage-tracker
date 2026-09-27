"""Data Health: read-only checks over the production database that surface
correctable data-quality problems (stray legacy provider ids, orphaned
gauge series, unpriced models, ...) plus in-app fixes for each, backed by
the shared repair logic in `app/services/maintenance/`.

See `base.py` for the `Check` contract, `registry.py` for the fixed list of
checks, and `jobs.py` for the single-flight scan/apply job registry the API
layer (`app/api/endpoints/data_health.py`) drives.
"""
