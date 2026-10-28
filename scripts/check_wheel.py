"""Check a pure-Python wheel without dependency installation or source-tree imports.

--build is opt-in: an already installed uv builds offline, without build isolation
or Python downloads. Missing build/runtime dependencies fail; nothing is installed.
Only the project's own reviewed wheel may be executed by this checker.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from check_release import ReleaseError, read_archive, regular_file, scan_tree

RESOURCES = {
    "quality_analysis/static/app.js": "工作",
    "quality_analysis/static/style.css": "{",
    "quality_analysis/templates/index.html": "质量",
}
ENTRY_POINT = "quality_analysis.cli:main"


def inspect_wheel(path: Path) -> dict[str, bytes]:
    payload = read_archive(path)
    metadata_names = [name for name in payload if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise ReleaseError("exactly one package metadata directory is required")
    directory = metadata_names[0].rsplit("/", 1)[0]
    metadata = BytesParser().parsebytes(payload[metadata_names[0]])
    if (metadata["Name"] != "quality-analysis-agent" or not metadata["Version"]
            or not metadata["Requires-Python"]):
        raise ReleaseError("unexpected distribution identity or missing Python requirement")
    expected_directory = "quality_analysis_agent-" + metadata["Version"] + ".dist-info"
    if directory != expected_directory or any(
        ".dist-info/" in name and not name.startswith(directory + "/") for name in payload
    ):
        raise ReleaseError("metadata directory identity mismatch")
    wheel = BytesParser().parsebytes(payload.get(directory + "/WHEEL", b""))
    if wheel["Root-Is-Purelib"] != "true" or not wheel.get("Wheel-Version", "").startswith("1."):
        raise ReleaseError("only purelib Wheel-Version 1 is supported")
    entries = configparser.ConfigParser(interpolation=None)
    entries.read_string(payload.get(directory + "/entry_points.txt", b"").decode())
    if not entries.has_section("console_scripts") or dict(entries["console_scripts"]) != {
        "quality": ENTRY_POINT
    }:
        raise ReleaseError("quality console entry point is absent or changed")
    for resource in RESOURCES:
        if not payload.get(resource):
            raise ReleaseError("wheel is missing a static/template resource")
    if "quality_analysis/__init__.py" not in payload or "quality_analysis/cli.py" not in payload:
        raise ReleaseError("wheel is missing the importable package")
    record_name = directory + "/RECORD"
    if record_name not in payload:
        raise ReleaseError("wheel RECORD is missing")
    rows = list(csv.reader(io.StringIO(payload[record_name].decode())))
    records = {}
    for row in rows:
        if len(row) != 3 or row[0] in records:
            raise ReleaseError("malformed or duplicate RECORD row")
        records[row[0]] = row[1:]
    if records.keys() != payload.keys() or records[record_name] != ["", ""]:
        raise ReleaseError("RECORD does not cover exactly the wheel payload")
    for name, content in payload.items():
        if name == record_name:
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        if records[name] != ["sha256=" + digest, str(len(content))]:
            raise ReleaseError("RECORD hash/size mismatch")
    return payload


# -I -S plus manually selected dependency directories avoids editable-install .pth
# hooks. The project package must come only from the staged wheel's site directory.
PROBE = r'''
import contextlib
import importlib.metadata
import importlib.resources
import io
import json
import pathlib
import sys

site = pathlib.Path(sys.argv[1]).resolve()
root = pathlib.Path(sys.argv[2]).resolve()
stdlib = [p for p in sys.path if p and not pathlib.Path(p).resolve().is_relative_to(root)]
sys.path[:] = [str(site), *stdlib, *json.loads(sys.argv[3])]
sys.dont_write_bytecode = True

def no_external_effects(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
        raise RuntimeError("wheel probe does not allow network or subprocesses")
sys.addaudithook(no_external_effects)

import quality_analysis
assert pathlib.Path(quality_analysis.__file__).resolve().is_relative_to(site)
dist = importlib.metadata.distribution("quality-analysis-agent")
assert pathlib.Path(dist.locate_file("")).resolve() == site
entry, = [ep for ep in dist.entry_points if ep.group == "console_scripts" and ep.name == "quality"]
assert entry.value == "quality_analysis.cli:main"
with contextlib.redirect_stdout(io.StringIO()) as output:
    try:
        entry.load()(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("entry point --help did not exit")
assert "analyze" in output.getvalue() and "serve" in output.getvalue()

from fastapi.testclient import TestClient
from quality_analysis.api import create_app
from quality_analysis.provider import SyntheticProvider

assets = {"/": "templates/index.html", "/static/app.js": "static/app.js",
          "/static/style.css": "static/style.css"}
app = create_app(pathlib.Path.cwd() / "probe-data", SyntheticProvider())
with TestClient(app) as client:
    for route, resource in assets.items():
        expected = importlib.resources.files("quality_analysis").joinpath(resource).read_bytes()
        assert expected
        response = client.get(route)
        assert response.status_code == 200 and response.content == expected
    health = client.get("/api/health").json()
    assert health["inference_checked"] is False
    assert len(health["graph_nodes"]) == 13
    assert health["provider"]["mode"] == "synthetic-test"
    assert client.get("/static/missing.js").status_code == 404
    assert client.get("/api/me").status_code == 401
for name, module in list(sys.modules.items()):
    if name == "quality_analysis" or name.startswith("quality_analysis."):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(site)
print(json.dumps({"wheel_import": True, "entry_point": True,
                  "static_templates": True, "source_free": True,
                  "inference_checked": False}))
'''


def clean_environment(temporary: Path) -> dict[str, str]:
    return {"HOME": str(temporary), "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(temporary), "PATH": "/usr/bin:/bin",
            "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
            "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"}


def probe_wheel(payload: dict[str, bytes], root: Path, temporary: Path) -> dict:
    site = temporary / "site"
    site.mkdir()
    for name, content in payload.items():
        target = site / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    # This is a purelib staging operation, not pip/uv install; use only dependencies
    # already present in the invoking interpreter. Never process .pth files.
    dependencies = sorted({sysconfig.get_path(key) for key in ("purelib", "platlib")})
    dependency_paths = [str(Path(path).resolve()) for path in dependencies if Path(path).is_dir()]
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", PROBE, str(site), str(root),
         json.dumps(dependency_paths)], cwd=temporary, env=clean_environment(temporary),
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise ReleaseError("source-free probe failed; verify existing interpreter dependencies")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ReleaseError("source-free probe returned invalid output") from None


def build_offline(root: Path, temporary: Path) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        raise ReleaseError("uv is not installed; provide an already built --wheel instead")
    _, issues = scan_tree(root)
    if issues:
        raise ReleaseError("public release scan failed before build")
    output = temporary / "built"
    output.mkdir()
    result = subprocess.run(
        [uv, "build", str(root), "--wheel", "--out-dir", str(output),
         "--python", sys.executable, "--offline", "--no-python-downloads",
         "--no-build-isolation", "--no-cache", "--no-config", "--no-create-gitignore"],
        cwd=temporary, env=clean_environment(temporary), capture_output=True,
        text=True, timeout=120, check=False,
    )
    if result.returncode:
        raise ReleaseError("offline build failed; no dependency installation/download attempted")
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseError("offline build must produce exactly one wheel")
    return wheels[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wheel", type=Path, help="Reviewed project dist/*.whl")
    mode.add_argument("--build", action="store_true", help="Explicit offline uv build; no install")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        with tempfile.TemporaryDirectory(prefix="quality-wheel-") as directory:
            temporary = Path(directory)
            if args.build:
                wheel = build_offline(root, temporary)
            else:
                candidate = args.wheel if args.wheel.is_absolute() else root / args.wheel
                relative = candidate.relative_to(root)
                if len(relative.parts) != 2 or relative.parts[0] != "dist":
                    raise ReleaseError("wheel must be directly under project dist/")
                wheel = regular_file(root, relative.as_posix())
            payload = inspect_wheel(wheel)
            result = probe_wheel(payload, root, temporary)
            print(json.dumps(result, ensure_ascii=False))
        print("PASS: wheel structure, RECORD, isolated imports, entry point, and web resources")
        return 0
    except (ReleaseError, OSError, ValueError, subprocess.SubprocessError,
            zipfile.BadZipFile, configparser.Error):
        print("FAIL: wheel/build/probe check failed; no external download or fallback")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
