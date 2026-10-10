# Single source of truth for the server version. Release Please bumps the
# literal below in lockstep with package.json / pyproject.toml (registered in
# release-please-config.json `extra-files`), so it stays correct inside the
# Docker image too — the image copies app/ but not the root package.json.
__version__ = "3.0.0-beta.8"  # x-release-please-version
