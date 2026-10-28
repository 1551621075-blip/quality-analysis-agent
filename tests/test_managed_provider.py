"""Managed-runtime unit guards; these tests are NOT real model acceptance."""
import fcntl
import hashlib
import json
import socket

import pytest

from quality_analysis.managed_provider import ManagedLocalProvider
from quality_analysis.schemas import DomainError


@pytest.fixture
def configured(tmp_path):
    def record(name):
        path = tmp_path / name
        path.write_bytes(b"explicit non-executable unit fixture:" + name.encode())
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    models = {}
    for role in ("generation", "embedding", "rerank"):
        rec = record(role + ".gguf")
        rec["bytes"] = (tmp_path / (role + ".gguf")).stat().st_size
        models[role] = rec
    config = {"binary": record("llama-server"), "libraries": [record("libggml.dylib")],
              "models": models, "lock_path": str(tmp_path / "serial.lock"), "port": 18080}
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(config))
    return path, config


def test_configured_files_and_cpu_only_command(configured):
    path, config = configured
    provider = ManagedLocalProvider(path)
    for record in [provider.config.binary, *provider.config.libraries, *provider.config.models.values()]:
        provider._verify(record)
    command = provider._command("generation")
    assert command[command.index("--device") + 1] == "none"
    assert command[command.index("--n-gpu-layers") + 1] == "0"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert provider.fingerprint.startswith("local-bge-cpu-")
    changed = dict(config)
    changed["models"] = dict(config["models"])
    changed["models"]["generation"] = {**config["models"]["generation"], "sha256": "a" * 64}
    path.write_text(json.dumps(changed))
    assert ManagedLocalProvider(path).fingerprint != provider.fingerprint


def test_hash_corruption_detected_after_cached_verification(configured):
    path, _ = configured
    provider = ManagedLocalProvider(path)
    record = provider.config.binary
    provider._verify(record)
    from pathlib import Path
    Path(record.path).write_bytes(b"changed fixture")
    with pytest.raises(DomainError) as error:
        provider._verify(record)
    assert error.value.code == "runtime_integrity"


def test_runtime_config_refuses_incomplete_roles_or_relative_paths(configured):
    path, config = configured
    config["models"].pop("embedding")
    path.write_text(json.dumps(config))
    with pytest.raises(DomainError) as error:
        ManagedLocalProvider(path)
    assert error.value.code == "runtime_config"


def test_active_os_lock_stops_before_starting_process(configured):
    path, config = configured
    provider = ManagedLocalProvider(path)
    with open(config["lock_path"], "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DomainError) as error:
            provider.embed(["unit fixture"])
    assert error.value.code == "provider_unavailable"


def test_occupied_port_is_not_trusted(configured):
    path, config = configured
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        config["port"] = listener.getsockname()[1]
        path.write_text(json.dumps(config))
        with pytest.raises(DomainError) as error:
            ManagedLocalProvider(path).embed(["unit fixture"])
    assert error.value.code == "provider_unavailable"


def test_thread_busy_is_explicit_and_not_automatic_retry(configured):
    path, _ = configured
    provider = ManagedLocalProvider(path)
    provider._thread_lock.acquire()
    try:
        with pytest.raises(DomainError) as error:
            provider.embed(["unit fixture"])
        assert error.value.code == "provider_unavailable"
    finally:
        provider._thread_lock.release()
