"""Exercise production updater refusal paths on a native host, without installing.

This source-only test driver patches release discovery/transport and install hooks
inside its own process. It adds no production bypass or test endpoint.
"""

import argparse
import hashlib
import io
import json
import platform
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from scripts.sidecar_pkg import self_update
from scripts.sidecar_pkg.signatures import verify_update


def installed_hashes(path: Path) -> dict[str, str]:
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    if not files:
        raise ValueError("Installed copy must contain files")
    hashes = {}
    for file in files:
        digest = hashlib.sha256()
        with file.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[str(file.relative_to(path) if path.is_dir() else file.name)] = digest.hexdigest()
    return hashes


def probe(installed: Path, payload: Path, bundle: Path, output: Path) -> bool:
    # A valid positive baseline prevents refusal-only tests from certifying a
    # broken verifier or an already-invalid input asset set.
    verify_update(payload, bundle)
    baseline = installed_hashes(installed)
    previous = installed.with_name(installed.name + ".previous")
    rollback = installed_hashes(previous) if previous.exists() else None
    payload_digest = installed_hashes(payload)[payload.name]
    report = {
        "tested_at": datetime.now(UTC).isoformat(),
        "os": platform.platform(),
        "architecture": platform.machine(),
        "payload_sha256": payload_digest,
        "driver": "source fixture; production updater refusal logic",
        "cases": [],
    }
    passed = True
    with tempfile.TemporaryDirectory(prefix="runway-native-refusal-") as temp:
        root = Path(temp)
        tampered = root / "tampered.zip"
        with payload.open("rb") as src, tampered.open("wb") as dest:
            while chunk := src.read(1024 * 1024):
                dest.write(chunk)
            # Preserve the original bytes, then append corruption deliberately.
            dest.write(b"tampered")
        malformed = root / "malformed.sigstore.json"
        malformed.write_text("not a signature bundle")
        for case in (
            "missing-bundle",
            "malformed-bundle",
            "tampered-payload",
            "oversized-response",
        ):
            if case == "oversized-response" and not hasattr(self_update, "MAX_ARCHIVE_BYTES"):
                report["cases"].append(
                    {"case": case, "status": "blocked", "reason": "Source revision predates #605"}
                )
                passed = False
                continue
            actual_payload = tampered if case == "tampered-payload" else payload
            actual_bundle = malformed if case == "malformed-bundle" else bundle
            name = self_update.resolve_asset_name("tray", "edge", None)
            assets = [
                {"name": n, "browser_download_url": "https://fixture.invalid/" + n}
                for n in (name, name + ".sha256")
            ]
            if case != "missing-bundle":
                assets.append(
                    {
                        "name": name + ".sigstore.json",
                        "browser_download_url": "https://fixture.invalid/bundle",
                    }
                )

            stages = {
                "archive_opened": False,
                "body_read": False,
                "checksum_requested": False,
                "signature_attempted": False,
                "signature_rejected": False,
                "lock_acquired": False,
            }

            class Response(io.BytesIO):
                def __init__(self, data: bytes, headers: dict | None = None):
                    super().__init__(data)
                    self.headers = headers or {}

                def read(self, size: int = -1) -> bytes:
                    if case == "oversized-response" and size != 0:
                        stages["body_read"] = True
                    return super().read(size)

            def transport(req, **kwargs):
                url = req.full_url
                if url.endswith(".sha256"):
                    stages["checksum_requested"] = True
                    digest = installed_hashes(actual_payload)[actual_payload.name]
                    return Response(digest.encode())
                if url.endswith("/bundle"):
                    return Response(actual_bundle.read_bytes())
                stages["archive_opened"] = True
                if case == "oversized-response":
                    return Response(b"", {"Content-Length": str(self_update.MAX_ARCHIVE_BYTES + 1)})
                return actual_payload.open("rb")

            def recorded_verifier(*args):
                stages["signature_attempted"] = True
                try:
                    self_update_verifier(*args)
                except self_update.UpdateVerificationError:
                    stages["signature_rejected"] = True
                    raise

            # Capture production callables before replacing them below; wrappers
            # must never resolve the patched attribute and recurse into themselves.
            self_update_verifier = self_update.verify_update
            single_flight = self_update._single_flight

            @contextmanager
            def recorded_single_flight():
                with single_flight() as acquired:
                    stages["lock_acquired"] = bool(
                        acquired and (root / "config" / self_update._LOCK_NAME).is_file()
                    )
                    yield acquired

            def never_install(*args, **kwargs):
                raise AssertionError("Refused update reached extraction or installation")

            try:
                with (
                    patch.object(self_update, "_is_frozen", return_value=True),
                    patch.object(self_update, "_is_docker", return_value=False),
                    patch.object(self_update, "running_from_disk_image", return_value=False),
                    patch.object(self_update, "_detect_target", return_value="tray"),
                    patch.object(self_update, "_sidecar_dir", return_value=root / "config"),
                    patch.object(self_update, "_single_flight", side_effect=recorded_single_flight),
                    patch.object(self_update, "check_once", return_value="edge build fixture"),
                    patch.object(
                        self_update,
                        "_get_release_json",
                        return_value={"tag_name": "edge", "assets": assets},
                    ),
                    patch.object(self_update, "_github_ssl_context", return_value=None),
                    patch.object(self_update.request, "urlopen", side_effect=transport),
                    patch.object(self_update, "verify_update", side_effect=recorded_verifier),
                    patch.object(self_update, "_extract", side_effect=never_install),
                    patch.object(self_update, "apply_update", side_effect=never_install),
                ):
                    installed_result = self_update.self_update(
                        "0.0.0+edge.fixture-old", "edge", restart=True
                    )
                unchanged = installed_hashes(installed) == baseline
                rollback_unchanged = (
                    installed_hashes(previous) if previous.exists() else None
                ) == rollback
                lock_released = not (root / "config" / self_update._LOCK_NAME).exists()
                if case == "oversized-response":
                    expected_stage = (
                        stages["archive_opened"]
                        and not stages["body_read"]
                        and not stages["checksum_requested"]
                    )
                elif case == "missing-bundle":
                    expected_stage = not stages["archive_opened"]
                else:
                    expected_stage = stages["signature_attempted"] and stages["signature_rejected"]
                success = (
                    installed_result is False
                    and unchanged
                    and rollback_unchanged
                    and lock_released
                    and stages["lock_acquired"]
                    and expected_stage
                )
                report["cases"].append(
                    {
                        "case": case,
                        "status": "pass" if success else "fail",
                        "installed_unchanged": unchanged,
                        "rollback_unchanged": rollback_unchanged,
                        "lock_released": lock_released,
                        "expected_refusal_stage": expected_stage,
                        "stages": stages,
                    }
                )
                passed = passed and success
            except Exception as exc:
                report["cases"].append(
                    {"case": case, "status": "fail", "error_type": type(exc).__name__}
                )
                passed = False
    output.write_text(json.dumps(report, indent=2) + "\n")
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-copy", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(not probe(args.installed_copy, args.payload, args.bundle, args.output))
