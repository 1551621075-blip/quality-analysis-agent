"""Explicit local llama.cpp subprocesses: one model at a time, no cloud fallback."""
from __future__ import annotations

import fcntl
import hashlib
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
from pydantic import Field, ValidationError, model_validator

from .provider import LocalProvider
from .schemas import DomainError, StrictModel, digest

ALIASES = {"generation": "qwen3-4b-instruct-2507", "embedding": "bge-m3",
           "rerank": "bge-reranker-v2-m3"}


class RuntimeFile(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelFile(RuntimeFile):
    bytes: int = Field(gt=0)


class RuntimeConfig(StrictModel):
    binary: RuntimeFile
    libraries: list[RuntimeFile] = Field(min_length=1, max_length=30)
    models: dict[str, ModelFile]
    lock_path: str = Field(min_length=1)
    port: int = Field(default=18080, ge=1024, le=65535)

    @model_validator(mode="after")
    def roles(self):
        if set(self.models) != set(ALIASES):
            raise ValueError("Exactly generation, embedding and rerank model files are required")
        paths = [self.binary.path, self.lock_path,
                 *(f.path for f in self.libraries), *(f.path for f in self.models.values())]
        if any(not Path(p).is_absolute() for p in paths) or len(set(paths)) != len(paths):
            raise ValueError("Runtime paths must be distinct absolute paths")
        if any(Path(f.path).parent != Path(self.binary.path).parent for f in self.libraries):
            raise ValueError("Runtime libraries must be beside the configured binary")
        return self


class ManagedLocalProvider(LocalProvider):
    """CPU is explicit: GPU startup failed on the validation Mac; never auto-fallback.

    Each request starts and stops the selected model while holding an OS lock.
    This trades latency for bounded residency on a 16 GiB machine. Config is local
    trusted operator configuration, not an API request or an executable script.
    """

    def __init__(self, config_path: str | Path):
        try:
            self.config = RuntimeConfig.model_validate_json(Path(config_path).read_text())
        except (OSError, ValueError, ValidationError):
            raise DomainError("runtime_config", "本地运行配置不可读或格式错误", 503) from None
        url = f"http://127.0.0.1:{self.config.port}"
        super().__init__(url, url, url, *ALIASES.values())
        self.fingerprint = "local-bge-cpu-" + digest({
            "binary": self.config.binary.sha256,
            "libraries": sorted(f.sha256 for f in self.config.libraries),
            "models": {role: model.sha256 for role, model in self.config.models.items()},
            "aliases": ALIASES, "protocol": "quality-llama-cpu-v10-evidence-contract", "context": 8192,
        })
        self._verified: dict[str, tuple] = {}
        self._thread_lock = threading.Lock()

    def _verify(self, record: RuntimeFile):
        path = Path(record.path)
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if path.is_symlink() or not path.is_file():
                raise ValueError("Not a regular runtime file")
            if isinstance(record, ModelFile) and stat.st_size != record.bytes:
                raise ValueError("Model size mismatch")
            if self._verified.get(record.path) == identity:
                return
            sha = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(block)
            if sha.hexdigest() != record.sha256:
                raise ValueError("Runtime content hash mismatch")
            after = path.stat()
            if identity != (after.st_dev, after.st_ino, after.st_size,
                            after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Runtime changed while hashing")
            self._verified[record.path] = identity
        except (OSError, ValueError):
            raise DomainError("runtime_integrity", "本地模型或推理引擎文件未通过完整性检查", 503) from None

    def _command(self, role: str) -> list[str]:
        command = [self.config.binary.path, "--model", self.config.models[role].path,
            "--alias", ALIASES[role], "--host", "127.0.0.1", "--port", str(self.config.port),
            "--parallel", "1", "--threads", "4", "--threads-batch", "4", "--threads-http", "2",
            "--n-gpu-layers", "0", "--device", "none", "--no-op-offload", "--flash-attn", "off",
            "--no-webui", "--no-slots", "--log-disable"]
        if role == "generation":
            command += ["--ctx-size", "8192", "--batch-size", "512", "--ubatch-size", "128",
                        "--jinja", "--temp", "0", "--seed", "42"]
        else:
            command += ["--ctx-size", "2048", "--batch-size", "2048", "--ubatch-size", "2048",
                        "--embedding", "--pooling", "cls" if role == "embedding" else "rank"]
            if role == "rerank":
                command += ["--rerank"]
        return command

    def _post(self, url: str, payload: dict) -> dict:
        role = next((r for r, alias in ALIASES.items() if payload.get("model") == alias), None)
        if role is None or not url.startswith(self.generation_url + "/"):
            raise DomainError("runtime_request", "请求不匹配配置的本地模型", 503)
        if not self._thread_lock.acquire(blocking=False):
            raise DomainError("provider_unavailable", "本地模型正忙，请稍后显式恢复", 503)
        try:
            return self._exclusive_post(role, url, payload)
        finally:
            self._thread_lock.release()

    def _exclusive_post(self, role: str, url: str, payload: dict) -> dict:
        lock_path = Path(self.config.lock_path)
        if lock_path.is_symlink() or not lock_path.parent.is_dir():
            raise DomainError("runtime_config", "运行锁目录无效", 503)
        try:
            lock = lock_path.open("a+")
        except OSError:
            raise DomainError("runtime_config", "运行锁不可写", 503) from None
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DomainError("provider_unavailable", "其他本地模型仍在运行", 503) from None
            for record in [self.config.binary, *self.config.libraries, self.config.models[role]]:
                self._verify(record)
            with socket.socket() as sock:
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", self.config.port)) == 0:
                    raise DomainError("provider_unavailable", "模型端口已占用，拒绝连接未知进程", 503)
            with tempfile.TemporaryDirectory(prefix="quality-model-") as temporary:
                environment = {"HOME": temporary, "TMPDIR": temporary, "XDG_CACHE_HOME": temporary,
                               "PATH": "/usr/bin:/bin", "LC_ALL": "en_US.UTF-8"}
                try:
                    process = subprocess.Popen(self._command(role), env=environment, cwd=temporary,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(lock.fileno(),))
                except OSError:
                    raise DomainError("provider_unavailable", "无法启动本地推理进程", 503) from None
                try:
                    deadline = time.monotonic() + 90
                    with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
                        while True:
                            if process.poll() is not None:
                                raise DomainError("provider_unavailable", "本地模型加载失败，未发送推理请求", 503)
                            try:
                                response = client.get(self.generation_url + "/health")
                                if response.status_code == 200 and response.json().get("status") == "ok":
                                    break
                            except (httpx.HTTPError, ValueError):
                                pass
                            if time.monotonic() >= deadline:
                                raise DomainError("provider_unavailable", "本地模型加载超时，未发送推理请求", 503)
                            time.sleep(0.1)
                    return super()._post(url, payload)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                    else:
                        process.wait()
