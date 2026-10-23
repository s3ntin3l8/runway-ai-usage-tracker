"""The server image ships the pure catalog without sidecar credential runtimes."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG_FILES = ("source_catalog.py", "source_catalog_data.py")


def test_server_image_includes_the_static_catalog_only(tmp_path):
    dockerfile = (ROOT / "Dockerfile").read_text()
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    package = tmp_path / "scripts" / "sidecar_pkg"
    package.mkdir(parents=True)
    for filename in CATALOG_FILES:
        source = f"scripts/sidecar_pkg/{filename}"
        assert source in dockerfile
        assert f"!{source}" in ignored
        shutil.copyfile(ROOT / source, package / filename)
    # Use Python namespace packages, exactly as in the image. No sidecar
    # __init__, credential adapter, native framework, or host home is needed.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from scripts.sidecar_pkg.source_catalog import catalog; "
            "assert 'anthropic.claude-code' in catalog(); "
            "assert all(source.platforms == ('darwin',) for source in catalog().values()); "
            "assert set(name for name in sys.modules if name.startswith('scripts.sidecar_pkg.')) "
            "== {'scripts.sidecar_pkg.source_catalog', 'scripts.sidecar_pkg.source_catalog_data'}",
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
