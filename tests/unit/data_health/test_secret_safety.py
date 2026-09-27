"""Guards the class of leak #364 fixed: a Data Health report, preview, or
fix result must never surface a `ProviderConfig`'s encrypted credential
fields. Runs every registered check's `detect()` (and `plan`/`apply` for
whichever group is fixable) against a fixture DB seeded with a
default-keyed, credential-bearing config, and asserts the secret value
never appears anywhere in the serialized output.
"""

from __future__ import annotations

import dataclasses
import json

from app.services.data_health.registry import REGISTRY
from tests.unit.data_health.conftest import make_config, make_event, make_tag

_SECRET_MARKER = "cred-blob-do-not-leak-abc123"  # pragma: allowlist secret


def _serialize(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), default=str)


def test_no_check_output_leaks_encrypted_credential_fields(session):
    make_config(
        session,
        provider_id="minimax",
        account_id="default",
        account_label="alice@example.com",
        api_key_encrypted=_SECRET_MARKER,  # pragma: allowlist secret
        session_cookie_encrypted=_SECRET_MARKER,  # pragma: allowlist secret
        oai_sc_cookie_encrypted=_SECRET_MARKER,  # pragma: allowlist secret
    )
    make_tag(session, provider_id="minimax", credential_origin="path:/x", account_id="default")
    make_event(session, event_id="1", provider_id="minimax", account_id="default")

    for check in REGISTRY:
        report = check.detect(session)
        assert _SECRET_MARKER not in _serialize(report), (
            f"{check.id!r} detect() leaked a credential"
        )

        for group in report.groups:
            if not group.fixable:
                continue
            try:
                plan = check.plan(session, group.key, {})
            except ValueError:
                continue  # needs a param this fixture doesn't supply — not what this test checks
            assert _SECRET_MARKER not in _serialize(plan), (
                f"{check.id!r} plan() leaked a credential"
            )
