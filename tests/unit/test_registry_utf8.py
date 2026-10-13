"""Registry JSON has a fixed UTF-8 encoding even on a legacy Windows code page."""

import builtins
import json

from app.core.registry import Registry


def test_registry_decodes_utf8_independently_of_the_default_code_page(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    name = "Provider \u2714 \u00b7 OAuth"
    path.write_text(
        json.dumps({"providers": {"test": {"name": name}}}, ensure_ascii=False), encoding="utf-8"
    )
    native_open = builtins.open

    def cp1252_default(file, *args, **kwargs):
        kwargs.setdefault("encoding", "cp1252")
        return native_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", cp1252_default)
    registry = Registry()
    registry._path = str(path)
    assert registry.get_provider("test")["name"] == name
