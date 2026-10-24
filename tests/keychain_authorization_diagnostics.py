"""Fixed, secret-free output schema for synthetic authorization metadata."""

FIELDS = (
    "metadata_status",
    "python_reader_identity_available",
    "helper_identity_available",
    "identity_partitions_equal",
    "trust_comparison_available",
    "python_reader_trust_blob_equal",
    "helper_trust_blob_equal",
    "broad_decrypt_access",
    "partition_acl_present",
    "partition_metadata_available",
    "partition_contains_python_reader_identity",
    "partition_contains_helper_identity",
)


def sanitize(value: object) -> dict:
    """Never serialize native strings, blobs, identities, or unexpected fields."""
    if not isinstance(value, dict) or type(value.get("metadata_status")) is not int:
        raise ValueError("invalid authorization metadata")
    if any(
        name not in value or (value[name] is not None and type(value[name]) is not bool)
        for name in FIELDS[1:]
    ):
        raise ValueError("invalid authorization metadata")
    return {name: value[name] for name in FIELDS}
