"""Archiving requires explicit authenticated POST and never dispatches source operations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.integration import test_claude_cloud_verification_endpoint as fixtures

client = fixtures.client
PATH, AUTH, HANDLE = fixtures.PATH, fixtures.AUTH, fixtures.HANDLE


def test_preview_and_archive_use_only_exact_local_checkpoint(client, monkeypatch):
    http, endpoint, advance = client
    calls = []
    store = SimpleNamespace(
        archive_preview=lambda handle: {"state": "terminal", "job_handle": handle, "revision": 7},
        archive=lambda handle, revision: (calls.append((handle, revision)), 7)[1],
    )
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: store)
    read = AsyncMock(side_effect=AssertionError("unexpected_source_access"))
    monkeypatch.setattr(endpoint, "read_verification", read)
    response = http.post(
        PATH, json={"action": "archive_status", "job_handle": HANDLE}, headers=AUTH
    )
    assert response.status_code == 200 and response.json()["revision"] == 7
    assert calls == []
    response = http.post(
        PATH, json={"action": "archive", "job_handle": HANDLE, "revision": 7}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"state": "archived", "job_handle": HANDLE, "revision": 7}
    assert calls == [(HANDLE, 7)]
    read.assert_not_awaited()
    advance.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"action": "archive", "job_handle": HANDLE, "revision": True},
        {"action": "archive", "job_handle": HANDLE, "revision": -1},
        {"action": "archive", "job_handle": HANDLE},
        {"action": "archive_status", "job_handle": HANDLE, "revision": 0},
    ],
)
def test_invalid_archive_inputs_are_inert(client, monkeypatch, body):
    http, endpoint, advance = client
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: pytest.fail("unexpected_storage"))
    assert http.post(PATH, json=body, headers=AUTH).status_code == 422
    advance.assert_not_awaited()


def test_archive_cannot_run_without_admin_or_through_get(client, monkeypatch):
    http, endpoint, advance = client
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: pytest.fail("unexpected_storage"))
    body = {"action": "archive", "job_handle": HANDLE, "revision": 0}
    assert http.post(PATH, json=body).status_code == 403
    assert http.get(PATH, params=body, headers=AUTH).status_code in (404, 405)
    advance.assert_not_awaited()


@pytest.mark.parametrize(
    "headers",
    [
        {**AUTH, "Host": "attacker.invalid"},
        {**AUTH, "Origin": "https://attacker.invalid"},
    ],
)
def test_archive_retains_host_and_csrf_protections(client, monkeypatch, headers):
    http, endpoint, advance = client
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: pytest.fail("unexpected_storage"))
    response = http.post(
        PATH, json={"action": "archive", "job_handle": HANDLE, "revision": 0}, headers=headers
    )
    assert response.status_code in (400, 403)
    advance.assert_not_awaited()


def test_archive_gate_prevents_mutation_and_errors_stay_sanitized(client, monkeypatch):
    http, endpoint, advance = client
    store = SimpleNamespace(archive=lambda *args: pytest.fail("unexpected_mutation"))
    monkeypatch.setattr(endpoint, "checkpoint_store", lambda: store)
    monkeypatch.setattr(endpoint.support, "gate", SimpleNamespace(locked=lambda: True))
    response = http.post(
        PATH, json={"action": "archive", "job_handle": HANDLE, "revision": 0}, headers=AUTH
    )
    assert response.status_code == 409 and response.json() == {"detail": "busy"}
    advance.assert_not_awaited()
