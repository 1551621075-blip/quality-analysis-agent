"""Create a local-only CPU runtime config after verifying the three public weights.

No downloads, server launch, installer, shell evaluation or model substitution.
The generated absolute paths are machine-local and must not be committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

MODELS = {
    "generation": ("Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2497281120,
                   "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"),
    "embedding": ("bge-m3-FP16.gguf", 1157671200,
                  "daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c"),
    "rerank": ("bge-reranker-v2-m3-FP16.gguf", 1159776896,
               "5df93be121c09c43432102ad2b9569d369ccb85c209ca7583e8ccd28f0e41b88"),
}


def record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular nonsymlink file: " + path.name)
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return {"path": str(path.absolute()), "sha256": sha.hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, help="Trusted locally built llama-server")
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--lock-dir", required=True, help="Existing shared runtime lock directory")
    parser.add_argument("--output", required=True, help="New JSON file OUTSIDE the project directory")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    output, binary = Path(args.output).absolute(), Path(args.binary).absolute()
    project = Path(__file__).resolve().parent.parent
    if output.resolve().is_relative_to(project) or output.exists() or output.is_symlink():
        raise ValueError("Use a new config path outside the project")
    if not output.parent.is_dir() or not 1024 <= args.port <= 65535:
        raise ValueError("Output parent and unprivileged port must be valid")
    locks = Path(args.lock_dir).absolute()
    if not locks.is_dir() or locks.is_symlink():
        raise ValueError("Lock directory must exist and must not be a symlink")
    if not os.access(binary, os.X_OK):
        raise ValueError("Runtime binary must be executable")
    models = {}
    for role, (name, size, expected) in MODELS.items():
        path = Path(args.models_dir).absolute() / name
        metadata = record(path)
        if path.stat().st_size != size or metadata["sha256"] != expected:
            raise ValueError("Pinned public weight verification failed: " + name)
        models[role] = {**metadata, "bytes": size}
    libraries = sorted([*binary.parent.glob("*.dylib"), *binary.parent.glob("*.so")])
    if not libraries:
        raise ValueError("Expected shared runtime libraries beside llama-server")
    config = {"binary": record(binary), "libraries": [record(p) for p in libraries],
              "models": models, "lock_path": str(locks / "serial.lock"), "port": args.port}
    with output.open("x") as stream:
        json.dump(config, stream, indent=2)
    print("Verified 3 pinned weights and recorded local runtime hashes; no server was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
