"""Validate the CLI's opaque device identifier without importing server settings."""


def normalize_device_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 256 or any(not 32 <= ord(char) <= 126 for char in value):
        return None
    return value
