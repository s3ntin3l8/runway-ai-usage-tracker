"""Native read routing and executable entrypoint, without accessing real credentials."""

import sys
from pathlib import Path

import pytest

from scripts.sidecar_pkg import keychain_client as module
from scripts.sidecar_pkg.keychain_authorization import HelperReadAPI
from scripts.sidecar_pkg.keychain_protocol import KeychainError


def test_default_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    client = module.KeychainClient()
    assert client._command == [str(module.native_helper_path())]
    assert client._renewal._command[-1].endswith("keychain_worker.py")
    client.close()
    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "_MEIPASS", str(tmp_path), raising=False)
    client = module.KeychainClient()
    assert client._command == [str(tmp_path / "keychain_helper" / "kc_helper")]
    assert client._renewal._command == [sys.executable, "--runway-keychain-worker"]
    client.close()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_use_python(monkeypatch, platform):
    monkeypatch.setattr(module.sys, "platform", platform)
    client = module.KeychainClient()
    assert client._command[-1].endswith("keychain_worker.py")
    assert client._renewal is None
    client.close()


@pytest.mark.parametrize(
    "mode,reason", [("success", None), ("deny", "denied"), ("hang", "timed_out")]
)
def test_darwin_starts_fake_helper(monkeypatch, tmp_path, mode, reason):
    fake = tmp_path / "kc_helper"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os,plistlib,struct,sys,time\n"
        "size=struct.unpack('>I',sys.stdin.buffer.read(4))[0]\n"
        "request=plistlib.loads(sys.stdin.buffer.read(size))\n"
        + ("time.sleep(20)\n" if mode == "hang" else "")
        + "result="
        + repr({"error": "denied"} if mode == "deny" else {"result": b"fixture"})
        + "\n"
        "data=plistlib.dumps(result,fmt=plistlib.FMT_BINARY)\n"
        "sys.stdout.buffer.write(struct.pack('>I',len(data))+data);sys.stdout.buffer.flush()\n"
        "sys.stdin.buffer.read()\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "native_helper_path", lambda: fake)
    client = module.KeychainClient(timeout=0.25 if mode == "hang" else 5)
    try:
        if reason:
            with pytest.raises(KeychainError, match=f"^{reason}$"):
                client.call("copy", {"service": "fixture"}, {}, group="fixture")
            assert client.access_blocked("fixture")
        else:
            assert client.call("copy", {"service": "fixture"}, {}, group="fixture") == b"fixture"
            assert client._process.args == [str(fake)]
    finally:
        client.close()


def test_renewal_reads_and_writes_stay_on_python_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    client = module.KeychainClient()
    calls = []

    def call(operation, query, values, **kwargs):
        calls.append((operation, query))
        return [{"persistent": b"journal"}] if "service" in query else {"data": b"fixture"}

    monkeypatch.setattr(client._renewal, "call", call)
    try:
        client.call("copy", {"service": "Runway Claude Code renewal"}, {}, group="code")
        client.call("copy", {"persistent": b"journal"}, {}, group="code")
        client.call(
            "update",
            {"persistent": b"credential"},
            {"data": b"x"},
            group="code",
            allowed=lambda: True,
        )
        assert len(calls) == 3
        assert client._process is None
    finally:
        client.close()


def test_renewal_worker_lifecycle_and_recovery_reference_routing(monkeypatch, tmp_path):
    import plistlib

    from scripts.sidecar_pkg.macos_keychain_store import RECOVERY_SERVICE

    snapshot = tmp_path / "snapshot.plist"
    snapshot.write_bytes(
        plistlib.dumps(
            {
                "journal": {"service": RECOVERY_SERVICE, "account": "fixture", "data": b"original"},
            }
        )
    )
    fault = tmp_path / "fault"
    worker = Path(__file__).resolve().parents[1] / "keychain_worker_fixture.py"
    monkeypatch.setattr(module.sys, "platform", "darwin")
    client = module.KeychainClient()
    client._renewal._command = [sys.executable, str(worker), str(snapshot), str(fault)]
    try:
        assert client.call("copy", {"service": RECOVERY_SERVICE}, {}, group="code") == [
            {"persistent": b"journal"}
        ]
        assert (
            client.call("copy", {"persistent": b"journal"}, {}, group="code")["data"] == b"original"
        )
        process = client._renewal._process
        client.call(
            "update",
            {"persistent": b"journal"},
            {"data": b"updated"},
            group="code",
            allowed=lambda: True,
        )
        assert plistlib.loads(snapshot.read_bytes())["journal"]["data"] == b"updated"
        assert client._process is None
        client.block_access("blocked")
        assert client._renewal.access_blocked("blocked")
        client.close()
        assert process.poll() is not None
        client.reopen()
        assert client.access_blocked("blocked")
        assert (
            client.call("copy", {"persistent": b"journal"}, {}, group="code")["data"] == b"updated"
        )
    finally:
        client.close()


def test_foreground_adapter_uses_native_identity(monkeypatch, tmp_path):
    from scripts.sidecar_pkg import keychain_authorization as authorization

    # The autouse fixture blocks the adapter constructor. Explicitly instantiate
    # its real implementation saved below, with a synthetic executable only.
    fake = tmp_path / "kc_helper"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import plistlib,struct,sys\n"
        "while True:\n"
        " h=sys.stdin.buffer.read(4)\n"
        " if not h: break\n"
        " q=plistlib.loads(sys.stdin.buffer.read(struct.unpack('>I',h)[0]))['query']\n"
        " item={'service':'fixture','account':'fixture-user','persistent':b'ref'}\n"
        " if 'persistent' in q: item['data']=b'fixture'\n"
        " r={'result':item if 'persistent' in q else [item]}\n"
        " d=plistlib.dumps(r,fmt=plistlib.FMT_BINARY)\n"
        " sys.stdout.buffer.write(struct.pack('>I',len(d))+d);sys.stdout.buffer.flush()\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(authorization, "native_helper_path", lambda: fake)
    api = HelperReadAPI()
    try:
        assert api.client._timeout == authorization.AUTHORIZATION_TIMEOUT
        authorization.authorize_once(api, ["fixture"], "fixture-user")
        process = api.client._process
        assert process.args == [str(fake), "--interactive"]
    finally:
        api.close()
    assert process.poll() is not None
