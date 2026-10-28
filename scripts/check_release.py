"""Scan only Git's public candidate list; never walk/read ignored runtime files.

This is a bounded release gate, not a general secret detector or license audit.
Exceptions are exact (path, rule, literal) triples for intentional rejection tests.
"""
from __future__ import annotations

import argparse
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 2000
ROOT_FILES = {".gitignore", "README.md", "pyproject.toml", "uv.lock"}
SCRIPTS = {
    "check_release.py", "check_wheel.py", "real_model_smoke.py",
    "real_model_analysis.py", "browser_smoke.mjs", "configure_runtime.py",
}
ASSETS = {"static/app.js", "static/style.css", "templates/index.html"}
METADATA_FILES = {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt"}
FORBIDDEN_PARTS = {
    ".git", ".venv", "venv", "runtime", "models", ".quality-data", "__pycache__",
    ".pytest_cache", ".ruff_cache", "node_modules", "private", "secrets", ".ssh",
    ".aws", ".config", "browser", "profiles",
}
FORBIDDEN_SUFFIXES = {
    ".gguf", ".safetensors", ".bin", ".onnx", ".pt", ".pth", ".pkl", ".pickle",
    ".db", ".log", ".pem", ".key", ".dylib", ".so", ".dll", ".exe", ".pyc",
}
RULES = {
    "private-home-link": re.compile(
        r"(?:file://)?/(?:Users|home)/[^\s\"'<>`)]+|/(?:root)/[^\s\"'<>`)]+"
        r"|[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'<>`)]+|[~]/[^\s\"'<>`)]+", re.I
    ),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "service-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,})\b"
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    "bearer-literal": re.compile(r"\bBearer\s+[A-Za-z0-9_+./=-]{20,}", re.I),
    "credential-url": re.compile(r"https?://[^\s/\"'<>]+@[^\s\"'<>),]+", re.I),
    "credential-query": re.compile(
        r"[?&](?:token|access_token|api_key|password|secret)=[^\s\"'<>),&]+", re.I
    ),
    "literal-credential": re.compile(
        r"[\"']?(?:QA_TOKEN|api[_-]?key|access[_-]?token|password|secret|token)[\"']?"
        r"\s*[:=]\s*[\"'][A-Za-z0-9_+./=-]{16,}[\"']", re.I
    ),
}
# No file-wide, directory-wide, or user-supplied suppression is supported.
EXCEPTIONS = {
    ("tests/test_provider.py", "credential-url", "http://" + "127.0.0.1@evil/"),
    ("tests/test_provider.py", "credential-url", "http://" + "user:pass@127.0.0.1:9"),
    ("tests/test_provider.py", "credential-query", "?" + "token=x"),
}


class ReleaseError(ValueError):
    pass


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ReleaseError("unsafe relative member name")
    return path


def allowed_path(name: str, *, wheel: bool = False) -> bool:
    path = safe_name(name)
    parts = path.parts
    lower = [part.lower() for part in parts]
    if (set(lower) & FORBIDDEN_PARTS or any(part.startswith(".env") for part in lower)
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or any(".sqlite" in part for part in lower)):
        return False
    package = "quality_analysis" if wheel else "src/quality_analysis"
    prefix = package + "/"
    if name.startswith(prefix):
        remainder = name[len(prefix):]
        return remainder in ASSETS or ("/" not in remainder and remainder.endswith(".py"))
    if wheel:
        return (len(parts) == 2 and re.fullmatch(
            r"quality_analysis_agent-[A-Za-z0-9_.!+]+\.dist-info", parts[0]
        ) is not None and parts[1] in METADATA_FILES)
    return (name in ROOT_FILES
            or len(parts) == 2 and (
                parts[0] == "docs" and parts[1].endswith(".md")
                or parts[0] == "scripts" and parts[1] in SCRIPTS
                or parts[0] == "tests" and re.fullmatch(r"test_[a-z0-9_]+\.py", parts[1])
            ))


def scan_content(name: str, content: bytes) -> list[str]:
    if len(content) > MAX_FILE_BYTES:
        return [f"{name}: file-size-limit"]
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return [f"{name}: non-text-payload"]
    if "\x00" in text:
        return [f"{name}: non-text-payload"]
    # Detect encoded home links too; report rule names, never matched secret values.
    text = unquote(unquote(text))
    return [f"{name}: {rule}" for rule, pattern in RULES.items()
            if any((name, rule, match.group()) not in EXCEPTIONS
                   for match in pattern.finditer(text))]


def regular_file(root: Path, name: str) -> Path:
    path = root
    for index, part in enumerate(safe_name(name).parts):
        path /= part
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ReleaseError(f"{name}: symlink-not-allowed")
        if index < len(PurePosixPath(name).parts) - 1 and not stat.S_ISDIR(mode):
            raise ReleaseError(f"{name}: non-directory-parent")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ReleaseError(f"{name}: regular-file-required")
    return path


def _git(root: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], input=data,
                          capture_output=True, check=False, timeout=30)


def scan_tree(root: Path) -> tuple[int, list[str]]:
    root = root.resolve(strict=True)
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.decode().strip()).resolve() != root:
        raise ReleaseError("root must be the exact Git project root")
    listing = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if listing.returncode:
        raise ReleaseError("cannot enumerate public Git candidate paths")
    names = sorted(set(name.decode("utf-8") for name in listing.stdout.split(b"\0") if name))
    if not names or len(names) > MAX_FILES:
        raise ReleaseError("empty candidate list or file-count-limit")
    # --no-index also detects force-added ignored files; reject WITHOUT opening them.
    ignored_result = _git(root, "check-ignore", "--no-index", "--stdin", "-z",
                          data=b"\0".join(name.encode() for name in names) + b"\0")
    if ignored_result.returncode not in {0, 1}:
        raise ReleaseError("cannot establish ignored-file boundary")
    ignored = set(ignored_result.stdout.decode().strip("\0").split("\0"))
    issues, total, checked = [], 0, 0
    for name in names:
        try:
            if name in ignored:
                issues.append(f"{name}: ignored-but-tracked; not-read")
                continue
            if not allowed_path(name):
                issues.append(f"{name}: outside-public-allowlist; not-read")
                continue
            path = regular_file(root, name)
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                issues.append(f"{name}: size-limit; not-read")
                continue
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            issues.extend(scan_content(name, content))
            checked += 1
        except (ReleaseError, OSError) as exc:
            issues.append(str(exc) if isinstance(exc, ReleaseError) else f"{name}: unreadable")
    return checked, issues


def read_archive(path: Path) -> dict[str, bytes]:
    """Validate and read bounded textual members; never extract archive paths."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOTAL_BYTES:
        raise ReleaseError("archive must be a bounded regular file")
    wheel = path.name.endswith(".whl")
    if not wheel and not path.name.endswith(".tar.gz"):
        raise ReleaseError("only .whl and .tar.gz archives are supported")
    result, seen, total = {}, set(), 0
    with zipfile.ZipFile(path) if wheel else tarfile.open(path, "r:gz") as archive:
        members = archive.infolist() if wheel else archive
        roots = set()
        for index, member in enumerate(members):
            if index >= MAX_FILES:
                raise ReleaseError("archive file-count-limit")
            raw_name = member.filename if wheel else member.name
            name = raw_name.rstrip("/")
            parts = safe_name(name).parts
            if name in seen:
                raise ReleaseError("duplicate archive member")
            seen.add(name)
            if wheel:
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ReleaseError("archive non-regular member")
                directory, size = member.is_dir(), member.file_size
                if member.flag_bits & 1:
                    raise ReleaseError("encrypted archive member")
            else:
                if not (member.isfile() or member.isdir()):
                    raise ReleaseError("archive links/devices are forbidden")
                directory, size = member.isdir(), member.size
                roots.add(parts[0])
                if (len(roots) != 1 or not re.fullmatch(
                    r"quality[-_]analysis[-_]agent-[A-Za-z0-9_.!+]+", parts[0]
                )):
                    raise ReleaseError("unexpected sdist root")
                name = "/".join(parts[1:])
            if directory:
                directories = ({"quality_analysis", "quality_analysis/static",
                                "quality_analysis/templates"} if wheel else {
                    "", "src", "src/quality_analysis", "src/quality_analysis/static",
                    "src/quality_analysis/templates", "docs", "tests", "scripts",
                })
                metadata_directory = wheel and re.fullmatch(
                    r"quality_analysis_agent-[A-Za-z0-9_.!+]+\.dist-info", name
                )
                if name not in directories and not metadata_directory:
                    raise ReleaseError("archive directory outside public allowlist")
                continue
            if not (name == "PKG-INFO" and not wheel) and not allowed_path(name, wheel=wheel):
                raise ReleaseError(f"{name}: archive-outside-public-allowlist; not-read")
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ReleaseError("archive uncompressed-size-limit")
            stream = archive.open(member) if wheel else archive.extractfile(member)
            if stream is None:
                raise ReleaseError("archive unreadable member")
            with stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            problems = scan_content(name, content)
            if problems:
                raise ReleaseError("; ".join(problems))
            result[name] = content
    if not result:
        raise ReleaseError("empty archive")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path, action="append", default=[],
                        help="Explicit project dist/*.whl or dist/*.tar.gz; never auto-discover")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        checked, issues = scan_tree(root)
        for supplied in args.archive:
            candidate = supplied if supplied.is_absolute() else root / supplied
            relative = candidate.relative_to(root)
            if len(relative.parts) != 2 or relative.parts[0] != "dist":
                raise ReleaseError("explicit release archives must be directly under project dist/")
            read_archive(regular_file(root, relative.as_posix()))
        for issue in issues:
            print(issue)
        print(f"public files checked: {checked}; explicitly checked archives: {len(args.archive)}")
        print("FAIL" if issues else "PASS (bounded allowlist scan, not a complete secret/license audit)")
        return int(bool(issues))
    except (ReleaseError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile,
            subprocess.SubprocessError):
        print("FAIL: release boundary, archive, or Git check failed; no payload printed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
