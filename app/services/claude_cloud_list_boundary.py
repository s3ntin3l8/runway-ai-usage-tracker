"""Two explicit read-only GETs for the recorded list boundary; never resume a job."""

import asyncio
import hmac
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.models.claude_cloud_probe import ClaudeCloudProbeRequest
from app.models.claude_cloud_verification import VerificationState
from app.services import claude_cloud_probe as probe
from app.services import claude_cloud_support as support
from app.services.claude_cloud_checkpoints import CheckpointError, CheckpointStore
from app.services.claude_cloud_verification import source_digest
from scripts.claude_cloud_list_boundary import (
    BoundaryDecodeError,
    decode_boundary_json,
    observe_list_boundary,
)

TOTAL_TIMEOUT = 20


class _BoundaryProbe(probe.Probe):
    def __init__(
        self,
        handle: str,
        store: CheckpointStore,
        state: VerificationState,
        client: httpx.AsyncClient,
    ):
        super().__init__(
            ClaudeCloudProbeRequest(
                account_id=state.account_id,
                source_id=state.source_id,
                organization_id=state.organization,
            ),
            client,
            total_byte_limit=2 * 1024 * 1024,
        )
        self.handle, self.store, self.state = handle, store, state
        self.profile_subject: str | None = None
        self.response_encoding = "unknown"
        self.json_state = "not_attempted"
        self.duplicate_json_keys = 0
        self.nonfinite_json_values = 0

    async def check_source(self) -> None:
        try:
            current = await asyncio.to_thread(self.store.snapshot, self.handle)
        except CheckpointError:
            raise probe.ProbeError("checkpoint_changed") from None
        if current != self.state:
            raise probe.ProbeError("checkpoint_changed")
        await super().check_source()

    async def get(
        self, path: str, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> Any:
        if self.report["requests"] >= 2:
            raise probe.ProbeError("unavailable")
        self.response_encoding = "unknown"
        self.json_state = "not_attempted"
        self.duplicate_json_keys = self.nonfinite_json_values = 0
        value = await super().get(path, headers, params)
        if path == "/api/oauth/profile" and isinstance(value, dict):
            account = value.get("account")
            self.profile_subject = (
                support.strict_uuid(account.get("uuid")) if isinstance(account, dict) else None
            )
        return value

    def _response_encoding(self, response: httpx.Response) -> str:
        encoding = super()._response_encoding(response)
        self.response_encoding = "identity" if encoding == "identity" else "unsupported"
        return encoding

    def _decode(self, body: bytearray) -> Any:
        try:
            value = decode_boundary_json(body)
        except BoundaryDecodeError as exc:
            self.json_state = exc.category
            self.duplicate_json_keys = exc.duplicates
            self.nonfinite_json_values = exc.nonfinite
            raise probe.ProbeError("unsupported_contract") from None
        self.json_state = "decoded"
        return value


def _empty() -> dict[str, Any]:
    return {
        "format_version": 1,
        "outcome": "unavailable",
        "stage": "checkpoint",
        "requests": 0,
        "http_status": None,
        "account_verified": False,
        "organization_verified": False,
        "original_list_pages": None,
        "original_listed_sessions": None,
        "boundary": None,
        "response_encoding": "unknown",
        "json_state": "not_attempted",
        "duplicate_json_keys": 0,
        "nonfinite_json_values": 0,
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
        "live_acceptance_passed": False,
        "provenance_verified": False,
    }


async def inspect_list_boundary(
    handle: str, store: CheckpointStore, client: httpx.AsyncClient
) -> dict[str, Any]:
    """A separately requested diagnostic; state remains stopped and byte-for-byte unchanged."""
    report = _empty()
    if support.gate.locked():
        report["outcome"] = "busy"
        return report
    attempt = None
    async with support.gate:
        try:
            with probe.probing():
                async with asyncio.timeout(TOTAL_TIMEOUT):
                    state = await asyncio.to_thread(store.snapshot, handle)
                    if not (
                        state.phase == "stopped"
                        and state.outcome == "unsupported_contract"
                        and not state.list_complete
                        and state.history_pages == 0
                        and state.subject is not None
                        and state.organization is not None
                    ):
                        report["outcome"] = "checkpoint_ineligible"
                        return report
                    # These are historical, already committed prefix counts, not fresh coverage.
                    report.update(
                        original_list_pages=state.list_pages,
                        original_listed_sessions=state.listed_sessions,
                    )
                    report["stage"] = "source"
                    try:
                        source = await probe.load_source(state.account_id, state.source_id)
                    except probe.ProbeError as exc:
                        if str(exc) == "expired":
                            raise
                        raise probe.ProbeError("source_changed") from None
                    if not hmac.compare_digest(
                        source_digest(bytes.fromhex(state.job_key), source), state.source_digest
                    ):
                        raise probe.ProbeError("source_changed")
                    attempt = _BoundaryProbe(handle, store, state, client)
                    attempt.tokens, attempt.expected_org, attempt.authority = source
                    async with probe.token_cache.using_source(
                        "anthropic", state.account_id, state.source_id
                    ):
                        report["stage"] = "profile"
                        headers = await attempt.identity()
                        if attempt.profile_subject != state.subject:
                            raise probe.ProbeError("identity_mismatch")
                        report["stage"] = "sessions"
                        params: dict[str, Any] = {"limit": 20}
                        if state.next_cursor is not None:
                            params["cursor"] = state.next_cursor
                        payload = await attempt.get("/v1/code/sessions", headers, params)
                        boundary = observe_list_boundary(
                            payload,
                            bytes.fromhex(state.job_key),
                            set(state.seen_sessions),
                            set(state.seen_cursors),
                        )
                        await attempt.check_source()
                        report.update(
                            outcome="observed",
                            stage="complete",
                            boundary=boundary,
                            account_verified=True,
                            organization_verified=True,
                        )
        except CheckpointError:
            report["outcome"] = "checkpoint_unavailable"
        except probe.ProbeError as exc:
            category = str(exc)
            report["outcome"] = (
                category
                if category
                in {
                    "checkpoint_changed",
                    "source_changed",
                    "identity_mismatch",
                    "identity_unavailable",
                    "expired",
                    "rejected",
                    "rate_limited",
                    "unavailable",
                    "unsupported_contract",
                    "response_limit",
                }
                else "unavailable"
            )
        except (TimeoutError, httpx.TimeoutException):
            report["outcome"] = "timeout"
        except (httpx.HTTPError, SQLAlchemyError):
            report["outcome"] = "unavailable"
        except (ValueError, RecursionError):
            report["outcome"] = "unsupported_contract"
    if attempt is not None:
        report.update(
            requests=attempt.report["requests"],
            http_status=attempt.report["http_status"],
            response_encoding=attempt.response_encoding,
            json_state=attempt.json_state,
            duplicate_json_keys=attempt.duplicate_json_keys,
            nonfinite_json_values=attempt.nonfinite_json_values,
        )
    return report
