"""Release gates use generated fixtures, never local runtime/config/model files."""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checks(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT / "scripts"))
    return importlib.import_module("check_release"), importlib.import_module("check_wheel")


def fixture_wheel(path, *, omit=None, tamper=False):
    metadata = "quality_analysis_agent-0.1.0.dist-info"
    files = {
        "quality_analysis/__init__.py": b"",
        "quality_analysis/cli.py": b"def main(): pass\n",
        "quality_analysis/static/app.js": b"'use strict';",
        "quality_analysis/static/style.css": b"body { color: black; }",
        "quality_analysis/templates/index.html": b"<!doctype html><title>fixture</title>",
        metadata + "/METADATA": (b"Metadata-Version: 2.3\nName: quality-analysis-agent\n"
                                b"Version: 0.1.0\nRequires-Python: >=3.12,<3.13\n"),
        metadata + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        metadata + "/entry_points.txt": b"[console_scripts]\nquality = quality_analysis.cli:main\n",
    }
    if omit:
        files.pop(omit)
    output = io.StringIO()
    writer = csv.writer(output)
    for name, content in files.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + encoded, len(content)])
    writer.writerow([metadata + "/RECORD", "", ""])
    files[metadata + "/RECORD"] = output.getvalue().encode()
    if tamper:
        files["quality_analysis/cli.py"] += b"# changed after hashing\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return files


@pytest.mark.parametrize("name", [
    "runtime/config.json", "models/local.gguf", ".env", "src/quality_analysis/local.bin",
    "docs/notes.txt", "secrets.json", "../README.md", "/README.md", "docs/../README.md",
    "docs\\private.md", "docs/x\n.md", "src/quality_analysis/__pycache__/x.pyc",
])
def test_release_rejects_nonpublic_paths(checks, name):
    release, _ = checks
    try:
        assert not release.allowed_path(name)
    except release.ReleaseError:
        pass


@pytest.mark.parametrize("name", [
    "README.md", "pyproject.toml", "uv.lock", "docs/validation.md", "tests/test_package.py",
    "scripts/check_release.py", "src/quality_analysis/static/app.js",
    "src/quality_analysis/templates/index.html", "src/quality_analysis/api.py",
])
def test_release_public_allowlist(checks, name):
    assert checks[0].allowed_path(name)


def test_release_secret_rules_do_not_print_payload(checks):
    release, _ = checks
    values = [
        "sk-" + "a" * 32,
        "ghp_" + "b" * 36,
        "-----BEGIN " + "PRIVATE KEY-----",
        "token = '" + "fixture-sensitive-" * 3 + "'",
        "https://" + "user:password@example.invalid/path",
        "Bearer " + "fixture-sensitive-" * 3,
    ]
    for value in values:
        findings = release.scan_content("README.md", value.encode())
        assert findings
        assert all(value not in finding for finding in findings)


@pytest.mark.parametrize("value", [
    "/" + "Users/fixture/private.json",
    "file:///" + "home/fixture/private.json",
    "%2F" + "Users%2Ffixture%2Fprivate.json",
    "C:" + "\\Users\\fixture\\private.json",
    "~" + "/private.json",
])
def test_release_private_home_links(checks, value):
    assert checks[0].scan_content("docs/runtime.md", value.encode())


def test_release_exception_is_exact_path_rule_and_literal(checks):
    release, _ = checks
    literal = "http://" + "user:pass@127.0.0.1:9"
    assert not release.scan_content("tests/test_provider.py", literal.encode())
    assert release.scan_content("README.md", literal.encode())
    assert release.scan_content("tests/test_provider.py", (literal + "9").encode())


def test_release_binary_size_and_symlink_boundaries(checks, tmp_path):
    release, _ = checks
    assert release.scan_content("README.md", b"\x00binary")
    assert release.scan_content("README.md", b"\xff")
    assert release.scan_content("README.md", b"x" * (release.MAX_FILE_BYTES + 1))
    (tmp_path / "target").write_text("not followed")
    (tmp_path / "README.md").symlink_to(tmp_path / "target")
    with pytest.raises(release.ReleaseError, match="symlink"):
        release.regular_file(tmp_path, "README.md")


def test_release_never_reads_ignored_or_unapproved_files(checks, tmp_path, monkeypatch):
    release, _ = checks
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("runtime/\n.env\n")
    (tmp_path / "README.md").write_text("public fixture")
    (tmp_path / "runtime").mkdir()
    private = tmp_path / "runtime" / "config.json"
    private.write_text("must never be read")
    (tmp_path / ".env").write_text("must never be read")
    unapproved = tmp_path / "private.json"
    unapproved.write_text("must never be read")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "runtime/config.json"], check=True)
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path not in {private, unapproved, tmp_path / ".env"}
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    checked, issues = release.scan_tree(tmp_path)
    assert checked == 2
    assert "runtime/config.json: ignored-but-tracked; not-read" in issues
    assert "private.json: outside-public-allowlist; not-read" in issues
    assert not any(".env" in issue for issue in issues)


def test_wheel_structure_resources_entrypoint_and_record(checks, tmp_path):
    _, wheel = checks
    path = tmp_path / "quality_analysis_agent-0.1.0-py3-none-any.whl"
    expected = fixture_wheel(path)
    assert wheel.inspect_wheel(path) == expected


@pytest.mark.parametrize("omit", [
    "quality_analysis/static/app.js", "quality_analysis/static/style.css",
    "quality_analysis/templates/index.html",
    "quality_analysis_agent-0.1.0.dist-info/entry_points.txt",
])
def test_wheel_rejects_missing_resources_and_entrypoint(checks, tmp_path, omit):
    release, wheel = checks
    path = tmp_path / "fixture.whl"
    fixture_wheel(path, omit=omit)
    with pytest.raises(release.ReleaseError):
        wheel.inspect_wheel(path)


def test_wheel_rejects_record_tampering(checks, tmp_path):
    release, wheel = checks
    path = tmp_path / "fixture.whl"
    fixture_wheel(path, tamper=True)
    with pytest.raises(release.ReleaseError, match="RECORD"):
        wheel.inspect_wheel(path)


@pytest.mark.parametrize("name", ["../escape.py", "models/model.gguf", "quality_analysis/local.so"])
def test_archive_rejects_nonpublic_member(checks, tmp_path, name):
    release, _ = checks
    path = tmp_path / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, "not read")
    with pytest.raises(release.ReleaseError):
        release.read_archive(path)


def test_archive_rejects_symlink(checks, tmp_path):
    release, _ = checks
    path = tmp_path / "fixture.whl"
    info = zipfile.ZipInfo("quality_analysis/api.py")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "elsewhere")
    with pytest.raises(release.ReleaseError, match="non-regular"):
        release.read_archive(path)


def test_archive_rejects_private_empty_directory(checks, tmp_path):
    release, _ = checks
    path = tmp_path / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("models/", b"")
        archive.writestr("quality_analysis/__init__.py", b"")
    with pytest.raises(release.ReleaseError, match="directory"):
        release.read_archive(path)


def test_archive_rejects_duplicate_member(checks, tmp_path):
    release, _ = checks
    path = tmp_path / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("quality_analysis/__init__.py", b"")
        with pytest.warns(UserWarning, match="Duplicate"):
            archive.writestr("quality_analysis/__init__.py", b"")
    with pytest.raises(release.ReleaseError, match="duplicate"):
        release.read_archive(path)


def test_sdist_inspection_does_not_extract(checks, tmp_path):
    release, _ = checks
    path = tmp_path / "fixture.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name in ("README.md", "PKG-INFO", "src/quality_analysis/__init__.py"):
            info = tarfile.TarInfo("quality_analysis_agent-0.1.0/" + name)
            content = b"original fixture"
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    assert len(release.read_archive(path)) == 3
    assert not (tmp_path / "quality_analysis_agent-0.1.0").exists()


def test_wheel_build_missing_uv_does_not_install(checks, tmp_path, monkeypatch):
    release, wheel = checks
    monkeypatch.setattr(wheel.shutil, "which", lambda _: None)
    with pytest.raises(release.ReleaseError, match="not installed"):
        wheel.build_offline(PROJECT, tmp_path)


def test_wheel_explicit_build_is_offline_and_no_install(checks, tmp_path, monkeypatch):
    _, wheel = checks
    monkeypatch.setattr(wheel.shutil, "which", lambda _: "/opt/fixture/uv")
    monkeypatch.setattr(wheel, "scan_tree", lambda _: (1, []))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("--out-dir") + 1])
        fixture_wheel(output / "fixture.whl")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(wheel.subprocess, "run", run)
    assert wheel.build_offline(PROJECT, tmp_path).is_file()
    command, options = calls[0]
    assert command[1] == "build"
    assert {"--offline", "--no-python-downloads", "--no-build-isolation", "--no-config"} <= set(command)
    assert "install" not in command and not options.get("shell", False)
    assert options["env"]["UV_OFFLINE"] == "1"
    assert "QA_TOKEN" not in options["env"]


def test_wheel_probe_uses_isolated_python_without_install(checks, tmp_path, monkeypatch):
    _, wheel = checks
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"source_free":true}')

    monkeypatch.setattr(wheel.subprocess, "run", run)
    assert wheel.probe_wheel({"quality_analysis/__init__.py": b""}, PROJECT, tmp_path)
    command, options = calls[0]
    assert command[:4] == [sys.executable, "-I", "-S", "-c"]
    assert options["cwd"] == tmp_path
    assert "PYTHONPATH" not in options["env"] and "QA_RUNTIME_CONFIG" not in options["env"]
    assert not options.get("shell", False)
