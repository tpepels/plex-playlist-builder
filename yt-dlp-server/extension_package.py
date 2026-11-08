"""Deterministic in-process Chrome extension ZIP from canonical sources."""
import io
import json
import re
import zipfile
from pathlib import Path

FILES = ("manifest.json", "service-worker.js", "options.html", "options.js", "README.md")
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def package_extension(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "")
    if manifest.get("manifest_version") != 3 or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid Chrome extension manifest")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in FILES:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (root / name).read_bytes())
    buffer.seek(0)
    return f"yt-dlp-server-chrome-extension-v{version}.zip", buffer
