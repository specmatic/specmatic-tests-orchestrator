from __future__ import annotations

import io
import zipfile
from pathlib import Path


def build_minimal_jar_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as jar:
        jar.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\nCreated-By: specmatic-tests-orchestrator\n",
        )
        jar.writestr("com/specmatic/placeholder.txt", "placeholder\n")
    return buffer.getvalue()


def write_minimal_jar(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_minimal_jar_bytes())
    return path
