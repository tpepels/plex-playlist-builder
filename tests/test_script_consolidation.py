"""No standalone scripts; build and maintenance behavior belongs to the package."""
import io
import zipfile
from pathlib import Path

import library_repair
from extension_package import FILES, package_extension

ROOT = Path(__file__).resolve().parents[1]


def test_script_directories_removed_and_docker_copies_canonical_sources():
    assert not (ROOT / "scripts").exists()
    assert not (ROOT / "tools").exists()
    docker = (ROOT / "Dockerfile").read_text()
    assert "COPY chrome-extension ./chrome-extension" in docker
    assert "library_repair.py extension_package.py" in docker
    assert "COPY dist" not in docker and "COPY tools" not in docker


def test_chrome_extension_generated_from_current_files_and_deterministic():
    root = ROOT / "chrome-extension"
    name, stream = package_extension(root)
    other_name, other_stream = package_extension(root)
    assert name == other_name == "yt-dlp-server-chrome-extension-v1.0.0.zip"
    assert stream.getvalue() == other_stream.getvalue()
    with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as archive:
        assert archive.namelist() == list(FILES)
        for item in FILES:
            assert archive.read(item) == (root / item).read_bytes()


def test_library_repair_domain_capabilities_and_old_options_preserved():
    assert callable(library_repair.main)
    assert callable(library_repair.prepare_and_publish)
    assert callable(library_repair.collect_groups)
    assert callable(library_repair.collection_metadata)
    assert callable(library_repair.probe_source)
    assert library_repair.is_single_video_url("https://youtu.be/abcdefg")
    assert not library_repair.is_single_video_url(
        "https://music.youtube.com/playlist?list=PLexample"
    )
    readme = (ROOT / "README.md").read_text()
    for option in ("--apply", "--url", "--include-singles",
                   "--refresh-metadata", "--music-root", "--import-subdir",
                   "--state-dir"):
        assert option in readme


def test_app_maintenance_command_skips_runtime_worker_start():
    source = (ROOT / "app.py").read_text()
    assert "if not MAINTENANCE_ONLY:" in source
    assert "    start_runtime_workers()" in source
    assert 'sys.argv[1] == "repair-library"' in source
    assert "repair_library(sys.argv[2:])" in source
