from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .schemas import Actor, ArtifactRef, DomainError, Role, canonical


def now() -> str:
    return datetime.now(UTC).isoformat()


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite"
        self.business_path = self.root / "business.sqlite"
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                  actor_id TEXT PRIMARY KEY, role TEXT NOT NULL, plants TEXT NOT NULL,
                  acl_version INTEGER NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES('data_revision','0');
                INSERT OR IGNORE INTO settings VALUES('index_revision','0');
                CREATE TABLE IF NOT EXISTS sessions(
                  session_id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,request_json TEXT NOT NULL,status TEXT NOT NULL,
                  state_json TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL,
                  UNIQUE(owner,request_id));
                CREATE TABLE IF NOT EXISTS rounds(
                  round_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(session_id),
                  ordinal INTEGER NOT NULL,status TEXT NOT NULL,manifest_json TEXT NOT NULL,
                  authorization_json TEXT NOT NULL,state_json TEXT NOT NULL,created_at TEXT NOT NULL,
                  UNIQUE(session_id,ordinal));
                CREATE TABLE IF NOT EXISTS entities(
                  entity_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,round_id TEXT NOT NULL,
                  kind TEXT NOT NULL,parent_id TEXT,payload_ref TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS entity_round ON entities(round_id,kind);
                CREATE TABLE IF NOT EXISTS actions(
                  actor_id TEXT NOT NULL,action_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                  response_json TEXT NOT NULL,PRIMARY KEY(actor_id,action_id));
                CREATE TABLE IF NOT EXISTS calls(
                  call_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,round_id TEXT NOT NULL,
                  kind TEXT NOT NULL,payload_hash TEXT NOT NULL,status TEXT NOT NULL,
                  request_ref TEXT NOT NULL,response_ref TEXT,error_code TEXT,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS action_rounds(
                  actor_id TEXT NOT NULL,action_id TEXT NOT NULL,session_id TEXT NOT NULL,
                  round_id TEXT NOT NULL,mode TEXT NOT NULL,start_version INTEGER NOT NULL,
                  outcome_json TEXT,PRIMARY KEY(actor_id,action_id));
                CREATE TABLE IF NOT EXISTS tool_calls(
                  call_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,round_id TEXT NOT NULL,
                  kind TEXT NOT NULL,status TEXT NOT NULL,request_ref TEXT NOT NULL,
                  response_ref TEXT,entity_id TEXT,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases(
                  session_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints(
                  session_id TEXT NOT NULL,version INTEGER NOT NULL,node TEXT NOT NULL,
                  state_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(session_id,version));
                CREATE TABLE IF NOT EXISTS audit(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,session_id TEXT,
                  action TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS caches(
                  cache_key TEXT PRIMARY KEY,manifest_json TEXT NOT NULL,artifact_ref TEXT NOT NULL,
                  created_at TEXT NOT NULL);
            """)

    def connect(self) -> Connection:
        db = sqlite3.connect(self.path, factory=Connection, isolation_level=None, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def provision_user(self, actor_id: str, role: Role | str, plants: list[str]) -> str:
        actor = Actor(actor_id=actor_id, role=role, plants=plants, acl_version=1)
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM users WHERE actor_id=?", (actor_id,)).fetchone():
                raise DomainError("user_exists", "用户已存在，不覆盖令牌", 409)
            db.execute("INSERT INTO users VALUES(?,?,?,?,?,1)",
                       (actor.actor_id, actor.role, canonical(actor.plants), 1,
                        hashlib.sha256(token.encode()).hexdigest()))
        return token

    def authenticate(self, token: str) -> Actor:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE token_hash=? AND active=1",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise DomainError("unauthorized", "身份令牌无效", 401)
        import json
        return Actor(actor_id=row["actor_id"], role=row["role"], plants=json.loads(row["plants"]),
                     acl_version=row["acl_version"])

    def current_actor(self, actor: Actor) -> Actor:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE actor_id=? AND active=1",
                             (actor.actor_id,)).fetchone()
        if row is None:
            raise DomainError("unauthorized", "身份已撤销", 401)
        import json
        current = Actor(actor_id=row["actor_id"], role=row["role"], plants=json.loads(row["plants"]),
                        acl_version=row["acl_version"])
        if current != actor:
            raise DomainError("acl_changed", "权限已变化，需重新认证", 409)
        return current

    def write_artifact(self, kind: str, payload: object) -> ArtifactRef:
        content = canonical(payload).encode()
        sha = hashlib.sha256(content).hexdigest()
        path = self.artifacts / (sha + ".json")
        try:
            with path.open("xb") as output:
                output.write(content)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise DomainError("artifact_integrity", "制品完整性异常", 409) from None
        return ArtifactRef(sha256=sha, kind=kind)

    def read_artifact(self, ref: ArtifactRef | dict):
        import json
        ref = ArtifactRef.model_validate(ref)
        path = self.artifacts / (ref.sha256 + ".json")
        if path.is_symlink() or not path.is_file():
            raise DomainError("artifact_missing", "制品不存在或不是普通文件", 409)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise DomainError("artifact_integrity", "制品哈希不匹配", 409)
        return json.loads(content)

    def revisions(self) -> tuple[int, int]:
        with self.connect() as db:
            data = dict(db.execute("SELECT key,value FROM settings"))
        return int(data["data_revision"]), int(data["index_revision"])

    @staticmethod
    def audit(db, actor: str, action: str, payload: object, session_id: str | None = None):
        db.execute("INSERT INTO audit(actor,session_id,action,payload,created_at) VALUES(?,?,?,?,?)",
                   (actor, session_id, action, canonical(payload), now()))
