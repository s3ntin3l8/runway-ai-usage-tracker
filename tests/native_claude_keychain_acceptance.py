"""Explicit native acceptance with synthetic credentials in a temporary Keychain.

Never collected by pytest. Run only with --isolated-synthetic-only and a packaged
Runway executable. Source workers use explicit private Keychains on every query.
Both source and packaged workers receive explicit scope through the private protocol.
"""

from __future__ import annotations

import argparse
import ctypes as c
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ACCOUNT = "runway-synthetic-acceptance"
DEFAULT = "Claude Code-credentials"
CUSTOM_DIR = "/Runway synthetic isolated acceptance"


def payload(*, refresh="synthetic-old-refresh", expiry: Any = 1) -> bytes:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "synthetic-old-access",
                "refreshToken": refresh,
                "expiresAt": expiry,
                "scopes": ["user:profile"],
            },
            "unrelated": {"preserve": True},
        }
    ).encode()


def environment_snapshot():
    """Fresh-process metadata avoids proving cleanup against a cached CF object."""
    source = (
        "import json; from tests.native_keychain_isolation import native_snapshot; "
        "print(json.dumps(native_snapshot()))"
    )
    command = [sys.executable, "-c", source]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=5, check=True)
    return json.loads(result.stdout)


class Fixture:
    def __init__(
        self, package: Path, *, source_native_io=True, protected=True, package_targets=False
    ):
        from scripts.sidecar_pkg.macos_keychain_store import _NativeAPI

        self.api = _NativeAPI()
        self.package = package
        self.source_native_io = source_native_io
        self.package_targets = package_targets
        self.refs = []
        self.keychains = []
        self.password = secrets.token_hex(24).encode()
        self.directory = tempfile.TemporaryDirectory(
            prefix="runway-native-526-",
            dir=Path.home() / "Library/Keychains" if protected else None,
        )
        self.cleanup = {}
        p = c.c_void_p
        for name, args in [
            ("SecKeychainCreate", [c.c_char_p, c.c_uint32, p, c.c_ubyte, p, c.POINTER(p)]),
            ("SecKeychainDelete", [p]),
            ("SecKeychainUnlock", [p, c.c_uint32, p, c.c_ubyte]),
            ("SecTrustedApplicationCreateFromPath", [c.c_char_p, c.POINTER(p)]),
            ("SecAccessCreate", [p, p, c.POINTER(p)]),
        ]:
            self.api._bind(self.api.sec, name, args, c.c_int32)
        self.api._bind(self.api.cf, "CFArrayCreate", [p, p, c.c_long, p], p)
        self.api._bind(self.api.cf, "CFDictionaryCreateMutableCopy", [p, c.c_long, p], p)
        self.api._bind(self.api.cf, "CFDictionarySetValue", [p, p, p], None)
        self.api._bind(self.api.cf, "CFEqual", [p, p], c.c_ubyte)

    @staticmethod
    def check(status):
        if status:
            error = RuntimeError("synthetic fixture operation failed")
            error.native_status = int(status)
            raise error

    def own(self, ref):
        if ref:
            self.refs.append(ref)
        return ref

    def array(self, values):
        p = c.c_void_p
        return self.own(
            self.api.cf.CFArrayCreate(None, (p * len(values))(*values), len(values), None)
        )

    def __enter__(self):
        self.before = environment_snapshot()
        try:
            self.add_keychain()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def add_keychain(self):
        kc = c.c_void_p()
        with self.api._without_interaction():
            self.check(
                self.api.sec.SecKeychainCreate(
                    str(
                        Path(self.directory.name) / f"fixture-{len(self.keychains)}.keychain"
                    ).encode(),
                    len(self.password),
                    self.password,
                    False,
                    None,
                    c.byref(kc),
                )
            )
        self.keychains.append(kc)
        return kc

    def paths(self):
        return sorted(Path(self.directory.name).glob("fixture-*.keychain*"))

    def command(self):
        return native_command(self.package, self.source_native_io)

    def unlock(self):
        for kc in self.keychains:
            self.check(self.api.sec.SecKeychainUnlock(kc, len(self.password), self.password, True))

    def add(self, service=DEFAULT, account=ACCOUNT, data=None, *, keychain=0):
        if self.package_targets:
            from scripts.sidecar_pkg.keychain_client import KeychainClient
            from scripts.sidecar_pkg.macos_keychain_store import RECOVERY_SERVICE
            from tests.native_keychain_isolation import validate_scope

            assert service == RECOVERY_SERVICE
            paths = validate_scope(Path(self.directory.name), [self.paths()[keychain]])
            scope = {"keychains": [str(path) for path in paths]}
            client = KeychainClient(command=self.command(), timeout=5)
            try:
                client.call(
                    "add",
                    scope,
                    {
                        "service": service,
                        "account": account,
                        "data": data if data is not None else payload(),
                    },
                    group="claude-code",
                    allowed=lambda: True,
                )
                matches = client.call(
                    "copy", dict(scope, service=service, account=account), {}, group="claude-code"
                )
                assert len(matches) == 1
                return matches[0]["persistent"]
            finally:
                client.close()
        api, p = self.api, c.c_void_p
        trusted = []
        for executable in (None, str(self.package)):
            app = p()
            self.check(
                api.sec.SecTrustedApplicationCreateFromPath(
                    executable.encode() if executable else None, c.byref(app)
                )
            )
            trusted.append(self.own(app))
        access = p()
        label = self.own(api._encode("Runway isolated synthetic acceptance"))
        self.check(api.sec.SecAccessCreate(label, self.array(trusted), c.byref(access)))
        self.own(access)
        attrs = self.own(
            api._encode(
                {
                    api.key("kSecClass"): api.key("kSecClassGenericPassword"),
                    api.key("kSecAttrService"): service,
                    api.key("kSecAttrAccount"): account,
                    api.key("kSecValueData"): data if data is not None else payload(),
                    api.key("kSecReturnPersistentRef"): True,
                    api.key("kSecUseDataProtectionKeychain"): False,
                }
            )
        )
        query = self.own(api.cf.CFDictionaryCreateMutableCopy(None, 0, attrs))
        for symbol, value in (
            ("kSecUseKeychain", self.keychains[keychain]),
            ("kSecAttrAccess", access),
        ):
            api.cf.CFDictionarySetValue(query, p.in_dll(api.sec, symbol), value)
        result = p()
        with api._without_interaction():
            self.check(api.sec.SecItemAdd(query, c.byref(result)))
        self.own(result)
        return c.string_at(api.cf.CFDataGetBytePtr(result), api.cf.CFDataGetLength(result))

    def __exit__(self, *args):
        try:
            for kc in self.keychains:
                self.check(self.api.sec.SecKeychainDelete(kc))
        finally:
            for ref in (*self.keychains, *reversed(self.refs)):
                if ref:
                    self.api.cf.CFRelease(ref)
            self.keychains.clear()
            self.directory.cleanup()
            self.cleanup = {
                "temporary_keychains_deleted": not Path(self.directory.name).exists(),
                "user_keychain_metadata_unchanged": environment_snapshot() == self.before,
            }
            assert all(self.cleanup.values()), "native fixture changed user Keychain metadata"


def native_command(package, source_native_io):
    if source_native_io:
        return [sys.executable, str(ROOT / "scripts/sidecar_pkg/keychain_worker.py")]
    return [str(package), "--runway-keychain-worker"]


def child(args):
    """Actual renewal/store with packaged native IO; provider responses are public fixtures."""
    import logging
    import plistlib

    from scripts.sidecar_pkg import claude_keychain_renewer as renewal
    from scripts.sidecar_pkg import keychain_client as clients
    from scripts.sidecar_pkg.macos_keychain_store import KeychainError, KeychainStore, _WorkerAPI

    if args.recovery_service_targets:
        global DEFAULT
        from scripts.sidecar_pkg.macos_keychain_store import RECOVERY_SERVICE

        DEFAULT = RECOVERY_SERVICE
        renewal.service_candidates = lambda unused: [RECOVERY_SERVICE]
    assert os.environ.get("RUNWAY_NATIVE_SYNTHETIC_ACCEPTANCE") == "1"
    os.environ.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    if args.suffix:
        os.environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = CUSTOM_DIR
    renewal.account_name = lambda: ACCOUNT
    client = clients.KeychainClient(
        command=native_command(args.package, args.source_native_io),
        timeout=5,
    )
    clients._CLIENT = client
    original = _WorkerAPI.call
    operation_count = 0
    fired = False
    messages = []

    class Handler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Handler()
    renewal.logger.addHandler(handler)
    renewal.logger.setLevel(logging.INFO)

    def call(self, operation, query, values):
        nonlocal operation_count, fired
        operation_count += 1
        stage = ""
        if operation == "add":
            stage = "intent"
        elif operation == "update":
            try:
                record = plistlib.loads(values.get("data", b""))
            except Exception:
                record = {}
            stage = "pending" if record.get("phase") == "pending" else "target"
        elif operation == "delete":
            stage = "cleanup"
        active = stage and not fired and args.mode.startswith(stage + "_")
        if active:
            fired = True
            if args.mode.endswith("before_save"):
                os._exit(77)
            if args.mode.endswith("failed"):
                raise KeychainError("failed")
        from tests.native_keychain_isolation import validate_scope

        paths = validate_scope(args.fixture_root, args.fixture_keychain)
        query = dict(query, keychains=[str(path) for path in paths])
        value = original(self, operation, query, values)
        if active and args.mode.endswith("after_save"):
            os._exit(77)
        return value

    _WorkerAPI.call = call

    def exchange(*unused):
        count = int(args.counter.read_text()) if args.counter.exists() else 0
        args.counter.write_text(str(count + 1))
        if args.mode == "response_lost":
            raise OSError("synthetic ambiguous response")
        if args.mode in {"concurrent_rotation", "deleted_target"}:
            service = DEFAULT
            if args.suffix:
                from scripts.sidecar_pkg.claude_code_keychain import service_candidates

                service = service_candidates([])[0]
            adapter = KeychainStore(allowed=lambda: True)
            item = adapter.find(service, ACCOUNT)
            assert item is not None
            if args.mode == "concurrent_rotation":
                adapter.update(
                    item.reference,
                    payload(refresh="synthetic-independent-refresh", expiry=9999999999999),
                )
            else:
                adapter.api.delete({"persistent": item.reference})
        return {
            "access_token": "synthetic-new-access",
            "refresh_token": "synthetic-new-refresh",
            "expires_in": 28800,
        }

    renewal.request_refresh = exchange
    worker = renewal.KeychainRenewer(lambda: [], lambda: not args.permission_off)
    try:
        result = worker.renew()
        if args.retry:
            result = worker.renew()
        forbidden = (
            "synthetic-old-access",
            "synthetic-old-refresh",
            "synthetic-new-access",
            "synthetic-new-refresh",
            ACCOUNT,
        )
        print(
            json.dumps(
                {
                    "renew_result": result,
                    "native_operations": operation_count,
                    "diagnostics_secret_free": all(
                        value not in " ".join(messages) for value in forbidden
                    ),
                    "diagnostics_deduplicated": len(messages) == len(set(messages)),
                    "outcomes": messages,
                    "fault_injected": fired,
                }
            )
        )
    finally:
        client.close()


def main(args):
    if args.recovery_service_targets:
        global DEFAULT
        from scripts.sidecar_pkg.macos_keychain_store import RECOVERY_SERVICE

        DEFAULT = RECOVERY_SERVICE
    from scripts.sidecar_pkg import keychain_client as clients
    from scripts.sidecar_pkg.claude_code_keychain import service_candidates
    from scripts.sidecar_pkg.macos_keychain_store import KeychainStore
    from tests.keychain_policy_fixture import capture, compare

    report = {
        "scope": "explicitly-scoped-native-synthetic",
        "packaged_native_io_exercised": not args.source_native_io,
        "protected_targets": args.source_native_io or args.recovery_service_targets,
        "recovery_service_targets": args.recovery_service_targets,
        "service_derivation_exercised": not args.recovery_service_targets,
        "real_credential_reads": 0,
        "real_credential_writes": 0,
        "real_oauth_exchanges": 0,
        "cases": [],
    }
    assert args.package.is_file()
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(args.package.parents[2])],
        check=True,
        capture_output=True,
    )
    active = []
    fixture = None

    def store():
        for value in active:
            value.close()
        client = clients.KeychainClient(
            command=fixture.command(),
            timeout=5,
        )
        active.append(client)
        clients._CLIENT = client
        from tests.native_keychain_isolation import scope_client_api

        return KeychainStore(
            api=scope_client_api(Path(fixture.directory.name), fixture.paths()),
            allowed=lambda: True,
        )

    try:
        cases = [
            "normal",
            "suffix",
            "intent_failed",
            "pending_before_save",
            "pending_after_save",
            "target_after_save",
            "cleanup_after_save",
            "pending_failed",
            "target_failed",
            "response_lost",
            "concurrent_rotation",
            "deleted_target",
            "permission_off",
            "malformed_expiry",
            "missing_expiry",
            "ambiguous",
        ]
        if args.recovery_service_targets:
            cases.remove("suffix")  # Actual Claude service names are covered in the other matrices.
        if args.cases:
            cases = [name for name in cases if name in args.cases.split(",")]
        for mode in cases:
            row = {"case": mode}
            fixture = Fixture(
                args.package,
                source_native_io=args.source_native_io,
                protected=args.source_native_io or args.recovery_service_targets,
                package_targets=args.recovery_service_targets,
            )
            try:
                with fixture:
                    raw = json.loads(payload())
                    if mode == "malformed_expiry":
                        raw["claudeAiOauth"]["expiresAt"] = "synthetic-malformed"
                    if mode == "missing_expiry":
                        raw["claudeAiOauth"].pop("expiresAt")
                    os.environ.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
                    service = DEFAULT
                    if mode == "suffix":
                        os.environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = CUSTOM_DIR
                        service = service_candidates([])[0]
                    reference = fixture.add(service, data=json.dumps(raw).encode())
                    decoy = fixture.add(service, account="synthetic-other-account")
                    default_decoy = fixture.add() if mode == "suffix" else None
                    if mode == "ambiguous":
                        fixture.add_keychain()
                        fixture.add(service=service, keychain=1)
                    initial_policy = (
                        capture(service, ACCOUNT, keychain=fixture.keychains[0])
                        if mode != "ambiguous"
                        else None
                    )
                    row["created_authorization_tags"] = (
                        sorted({tag for entry in initial_policy.entries for tag in entry[0]})
                        if initial_policy
                        else []
                    )
                    adapter = store()
                    before = adapter.read(reference)
                    decoy_before = adapter.read(decoy)
                    policy_before = (
                        capture(service, ACCOUNT, keychain=fixture.keychains[0])
                        if mode != "ambiguous"
                        else None
                    )
                    counter = Path(fixture.directory.name) / "fake-exchange-count"

                    def run(child_mode, **flags):
                        command = [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--child",
                            "--package",
                            str(args.package),
                            "--mode",
                            child_mode,
                            "--counter",
                            str(counter),
                        ]
                        if args.source_native_io:
                            command.append("--source-native-io")
                        if args.recovery_service_targets:
                            command.append("--recovery-service-targets")
                        if mode == "suffix":
                            command.append("--suffix")
                        for name, enabled in flags.items():
                            if enabled:
                                command.append("--" + name.replace("_", "-"))
                        command.extend(["--fixture-root", fixture.directory.name])
                        for path in fixture.paths():
                            command.extend(["--fixture-keychain", str(path)])
                        env = dict(os.environ, RUNWAY_NATIVE_SYNTHETIC_ACCEPTANCE="1")
                        result = subprocess.run(command, env=env, capture_output=True, timeout=15)
                        assert result.returncode in (0, 77), "synthetic child failed"
                        return (
                            json.loads(result.stdout)
                            if result.returncode == 0
                            else {"simulated_crash": True}
                        )

                    first_mode = "target_failed" if mode == "permission_off" else mode
                    row["first"] = run(
                        first_mode,
                        retry=mode in {"pending_failed", "malformed_expiry", "missing_expiry"},
                    )
                    count_before = int(counter.read_text()) if counter.exists() else 0
                    fixture.unlock()
                    adapter = store()
                    if mode == "permission_off":
                        pending = adapter.read_journal(reference)
                        assert pending is not None and pending["phase"] == "pending"
                        row["disabled"] = run("normal", permission_off=True)
                        assert row["disabled"]["native_operations"] == 0
                        assert store().read_journal(reference) == pending
                    if mode not in {"malformed_expiry", "missing_expiry", "ambiguous"}:
                        row["restart"] = run("normal")
                    adapter = store()
                    count_after = int(counter.read_text()) if counter.exists() else 0
                    row["fake_exchanges_before_restart"] = count_before
                    row["fake_exchanges_after_restart"] = count_after
                    assert count_after == (1 if mode == "intent_failed" else count_before)
                    assert count_after <= 1
                    if mode == "deleted_target":
                        assert adapter.find(service, ACCOUNT) is None
                        assert adapter.read_journal(reference)["phase"] == "pending"
                        row["deleted_target_not_recreated"] = True
                    else:
                        after = adapter.read(reference)
                        oauth = json.loads(after.data)["claudeAiOauth"]
                        if mode in {"pending_before_save", "response_lost"}:
                            assert oauth["refreshToken"] == "synthetic-old-refresh"
                            assert adapter.read_journal(reference)["phase"] == "intent"
                        elif mode in {"malformed_expiry", "missing_expiry", "ambiguous"}:
                            assert count_after == 0 and after == before
                            assert adapter.read_journal(reference) is None
                        elif mode == "concurrent_rotation":
                            assert oauth["refreshToken"] == "synthetic-independent-refresh"
                            assert adapter.read_journal(reference) is None
                        else:
                            assert oauth["refreshToken"] == "synthetic-new-refresh"
                            assert adapter.read_journal(reference) is None
                        if policy_before is not None:
                            row["policy"] = compare(
                                policy_before,
                                capture(service, ACCOUNT, keychain=fixture.keychains[0]),
                            )
                        if policy_before is not None:
                            assert row["policy"]["access_policy_preserved"]
                        assert json.loads(after.data)["unrelated"] == {"preserve": True}
                    for phase in ("first", "restart", "disabled"):
                        result = row.get(phase, {})
                        if not result.get("simulated_crash") and result:
                            assert result["diagnostics_secret_free"]
                            assert result["diagnostics_deduplicated"]
                    assert adapter.read(decoy) == decoy_before
                    if default_decoy is not None:
                        assert adapter.read(default_decoy).data == payload()
                    row["unselected_items_preserved"] = True
                    for value in active:
                        value.close()
                row["cleanup"] = fixture.cleanup
                row["outcome"] = "passed"
            except Exception as exc:
                row["outcome"] = "failed"
                row["error_type"] = type(exc).__name__
                row["native_status"] = getattr(exc, "native_status", None)
                import traceback

                row["error_locations"] = [
                    (f.name, f.lineno) for f in traceback.extract_tb(exc.__traceback__)
                ]
                row["cleanup"] = fixture.cleanup
                report["cases"].append(row)
                raise
            report["cases"].append(row)
            print(json.dumps({"case": mode, "outcome": "passed"}), flush=True)
        report["outcome"] = "passed"
    except Exception as exc:
        report["outcome"] = "failed"
        report["error_type"] = type(exc).__name__
    finally:
        for value in active:
            value.close()
        os.environ.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        report.setdefault("outcome", "interrupted")
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"outcome": report["outcome"], "cases": len(report["cases"])}))
    return 0 if report["outcome"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-synthetic-only", action="store_true")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--source-native-io",
        action="store_true",
        help="Use source helper for protected same-writer fixtures",
    )
    parser.add_argument(
        "--recovery-service-targets",
        action="store_true",
        help="Protected packaged-owned synthetic targets under the Runway service",
    )
    parser.add_argument("--cases", help="Optional comma-separated synthetic cases")
    parser.add_argument("--fixture-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--fixture-keychain", type=Path, action="append", default=[], help=argparse.SUPPRESS
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", default="normal", help=argparse.SUPPRESS)
    parser.add_argument("--counter", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--suffix", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--retry", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--permission-off", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    options.package = options.package.resolve()
    if sys.platform != "darwin" or not (options.isolated_synthetic_only or options.child):
        parser.error("requires macOS and explicit isolated synthetic execution")
    if options.source_native_io and options.recovery_service_targets:
        parser.error("recovery service targets require the packaged worker")
    if options.child:
        from tests.native_keychain_isolation import validate_scope

        validate_scope(options.fixture_root, options.fixture_keychain)
    if options.child:
        child(options)
    else:
        if options.report is None:
            parser.error("--report required")
        raise SystemExit(main(options))
