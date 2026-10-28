"""Local, ACL-first retrieval over immutable source versions.

The injected provider is the only component allowed to perform network I/O.  This
module neither downloads a model nor substitutes a cloud or lexical backend.
Scores are not probabilities. Until explicitly calibrated, every result is marked
as a low-confidence prior, even when exploratory candidates are returned.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import numbers
import re
from collections import defaultdict
from pathlib import PurePath
from typing import Any, Protocol

from pydantic import ValidationError

from .schemas import (
    Actor,
    DocumentInput,
    DomainError,
    RetrievalHit,
    RetrievalResult,
    Role,
    Scope,
    canonical,
    digest,
)
from .storage import Store, now

SUMMARY_CHANNELS = ("phenomenon", "context", "hypothesis", "evidence", "action")
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_SOURCE_CHARS = 200_000
MAX_PDF_PAGES = 200
MAX_DOCUMENTS = 64
MAX_VERSIONS = 32
MAX_CHUNKS_PER_DOCUMENT = 384
MAX_INDEX_CHUNKS = 4096
MAX_EMBEDDING_DIM = 4096
MAX_QUERY_CHARS = 2000
CHUNK_SIZE = 700
CHUNK_OVERLAP = 100
EMBED_BATCH_SIZE = 32
MAX_CANDIDATES = 64
MAX_TOP_K = 20
MAX_ISSUED_HITS = 4096
EXPLORATORY_TOP_K = 4  # A resource bound, not a claimed calibrated production setting.


class LocalRetrievalProvider(Protocol):
    fingerprint: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def rerank(self, query: str, texts: list[str]) -> list[float]: ...


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_text(text: str) -> str:
    if not text.strip():
        raise DomainError("empty_document", "The source contains no extractable text")
    if len(text) > MAX_SOURCE_CHARS:
        raise DomainError("document_too_large", "Extracted text exceeds the character limit", 413)
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise DomainError("invalid_text", "Source is not valid Unicode text") from exc
    if "\x00" in text or any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
        raise DomainError("invalid_text", "Source contains binary control characters")
    return text  # Never normalize whitespace: citation offsets refer to this exact string.


def parse_upload(filename: str, content: bytes) -> str:
    """Parse UTF-8 txt/md or text PDF; offsets subsequently refer to extracted text.

    No OCR, password guessing, external-resource fetch, or file-system access is
    performed. For PDFs, pages are joined with a single newline in source order.
    """
    if not isinstance(filename, str) or not isinstance(content, bytes):
        raise DomainError("invalid_upload", "A filename and bytes are required")
    if len(content) > MAX_UPLOAD_BYTES:
        raise DomainError("upload_too_large", "Upload exceeds the byte limit", 413)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in {".txt", ".md", ".pdf"}:
        raise DomainError("unsupported_upload", "Only UTF-8 txt/md and text PDF are supported", 415)
    if not content:
        raise DomainError("empty_document", "Upload is empty")
    if suffix in {".txt", ".md"}:
        try:
            return _source_text(content.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as exc:
            raise DomainError("invalid_encoding", "Text uploads must be UTF-8") from exc
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise DomainError("encrypted_pdf", "Encrypted PDFs are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DomainError("document_too_large", "PDF exceeds the page limit", 413)
        pages = []
        length = 0
        for page in reader.pages:
            # Bound the usual decompressed-content bomb before asking the text parser.
            stream = page.get_contents()
            if stream is not None and len(stream.get_data()) > MAX_UPLOAD_BYTES:
                raise DomainError("document_too_large", "PDF page content is too large", 413)
            text = page.extract_text() or ""
            length += len(text) + 1
            if length > MAX_SOURCE_CHARS:
                raise DomainError("document_too_large", "Extracted PDF is too large", 413)
            pages.append(text)
        return _source_text("\n".join(pages))
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("invalid_pdf", "PDF could not be parsed as a text document") from exc


def chunk_source(text: str) -> list[tuple[int, int, str]]:
    """Deterministic overlapping character slices, including original whitespace."""
    _source_text(text)
    result = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        result.append((start, end, text[start:end]))
        if len(result) > MAX_CHUNKS_PER_DOCUMENT:
            raise DomainError("chunk_limit", "Source exceeds the per-document chunk limit", 413)
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return result


def _fingerprint(provider: LocalRetrievalProvider) -> str:
    value = provider.fingerprint
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise DomainError("invalid_provider", "Provider must have a bounded, nonempty fingerprint", 503)
    return value


def _finite_number(value: Any) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, numbers.Real)
                and math.isfinite(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _vectors(raw: Any, count: int, dimension: int | None = None) -> tuple[list[list[float]], int]:
    if not isinstance(raw, list) or len(raw) != count or count < 1:
        raise DomainError("invalid_embedding", "Embedding batch cardinality mismatch", 503)
    result = []
    for vector in raw:
        if not isinstance(vector, list) or not 1 <= len(vector) <= MAX_EMBEDDING_DIM:
            raise DomainError("invalid_embedding", "Embedding dimension is invalid", 503)
        dimension = dimension or len(vector)
        if len(vector) != dimension or any(not _finite_number(x) for x in vector):
            raise DomainError("invalid_embedding", "Embeddings must be finite and same-dimensional", 503)
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise DomainError("invalid_embedding", "Embeddings must have finite nonzero norm", 503)
        result.append([float(x / norm) for x in vector])
    return result, int(dimension)


def _scores(raw: Any, count: int) -> list[float]:
    if not isinstance(raw, list) or len(raw) != count or any(not _finite_number(x) for x in raw):
        raise DomainError("invalid_rerank", "Reranker must return one finite score per candidate", 503)
    return [float(x) for x in raw]


def _cosine(left: list[float], right: list[float]) -> float:
    # Both inputs have been normalized and dimension checked.
    return max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(left, right, strict=True))))


class DocumentStore:
    def __init__(self, store: Store, provider: LocalRetrievalProvider):
        self.store, self.provider = store, provider
        _fingerprint(provider)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS document_heads(
                  document_id TEXT PRIMARY KEY, active_version INTEGER,
                  plant_id TEXT NOT NULL, shared_with TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS document_versions(
                  document_id TEXT NOT NULL REFERENCES document_heads(document_id),
                  version INTEGER NOT NULL, title TEXT NOT NULL, plant_id TEXT NOT NULL,
                  source_text TEXT NOT NULL, content_hash TEXT NOT NULL,
                  summaries_json TEXT NOT NULL, source_manifest_hash TEXT NOT NULL,
                  provider_fingerprint TEXT NOT NULL, dimension INTEGER NOT NULL,
                  created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(document_id,version));
                CREATE TABLE IF NOT EXISTS document_chunks(
                  document_id TEXT NOT NULL, version INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                  quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL, text TEXT NOT NULL,
                  PRIMARY KEY(document_id,version,ordinal),
                  FOREIGN KEY(document_id,version) REFERENCES document_versions(document_id,version));
                CREATE TABLE IF NOT EXISTS document_vectors(
                  document_id TEXT NOT NULL, version INTEGER NOT NULL, channel TEXT NOT NULL,
                  ordinal INTEGER NOT NULL, vector_json TEXT NOT NULL,
                  PRIMARY KEY(document_id,version,channel,ordinal),
                  FOREIGN KEY(document_id,version) REFERENCES document_versions(document_id,version));
                CREATE TABLE IF NOT EXISTS document_issued_hits(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, hit_hash TEXT NOT NULL,
                  actor_id TEXT NOT NULL, acl_version INTEGER NOT NULL,
                  index_revision INTEGER NOT NULL, provider_fingerprint TEXT NOT NULL,
                  document_id TEXT NOT NULL, chunk_id TEXT NOT NULL, hit_json TEXT NOT NULL,
                  UNIQUE(hit_hash,actor_id,acl_version,index_revision));
                CREATE INDEX IF NOT EXISTS document_issued_lookup
                  ON document_issued_hits(actor_id,document_id,chunk_id);
            """)

    @staticmethod
    def _current(db, actor: Actor) -> Actor:
        row = db.execute("SELECT * FROM users WHERE actor_id=? AND active=1",
                         (actor.actor_id,)).fetchone()
        if row is None:
            raise DomainError("unauthorized", "Identity has been revoked", 401)
        current = Actor(actor_id=row["actor_id"], role=row["role"],
                        plants=json.loads(row["plants"]), acl_version=row["acl_version"])
        if current != actor:
            raise DomainError("acl_changed", "Authorization changed; authenticate again", 409)
        return current

    @staticmethod
    def _allowed(row, actor: Actor) -> bool:
        shared = json.loads(row["shared_with"])
        return row["plant_id"] in actor.plants and (not shared or actor.actor_id in shared)

    def _head(self, db, document_id: str, actor: Actor, *, active: bool = True):
        row = db.execute("SELECT * FROM document_heads WHERE document_id=?",
                         (document_id,)).fetchone()
        if row is None or (active and row["active_version"] is None) or not self._allowed(row, actor):
            # Existence is deliberately not disclosed for denied documents.
            raise DomainError("document_unavailable", "Document is unavailable", 404)
        return row

    @staticmethod
    def _admin(actor: Actor, plant_id: str):
        if actor.role != Role.ADMIN or plant_id not in actor.plants:
            raise DomainError("forbidden", "Current plant administrator authorization is required", 403)

    @staticmethod
    def _revision(db) -> int:
        return int(db.execute("SELECT value FROM settings WHERE key='index_revision'").fetchone()[0])

    def _invalidate(self, db) -> int:
        db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='index_revision'")
        db.execute("DELETE FROM document_issued_hits")
        # Keep the old record for provenance, but matching revision is mandatory at use.
        return self._revision(db)

    def _embed(self, texts: list[str], fingerprint: str) -> tuple[list[list[float]], int]:
        result, dimension = [], None
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start:start + EMBED_BATCH_SIZE]
            try:
                raw = self.provider.embed(batch)
            except DomainError:
                raise
            except Exception as exc:
                raise DomainError("provider_unavailable", "Local embedding provider failed", 503) from exc
            vectors, dimension = _vectors(raw, len(batch), dimension)
            if _fingerprint(self.provider) != fingerprint:
                raise DomainError("provider_changed", "Provider changed during embedding", 409)
            result.extend(vectors)
        return result, int(dimension)

    def ingest(self, document: DocumentInput, actor: Actor) -> dict:
        document = DocumentInput.model_validate(
            document.model_dump() if isinstance(document, DocumentInput) else document
        )
        _source_text(document.text)
        if any(not value.strip() for value in document.summaries.values()):
            raise DomainError("invalid_summaries", "Five nonempty human-authored summaries are required")
        if len(document.shared_with) != len(set(document.shared_with)):
            raise DomainError("invalid_acl", "Duplicate ACL members are not allowed")
        chunks = chunk_source(document.text)
        fingerprint = _fingerprint(self.provider)
        with self.store.transaction() as db:
            self._current(db, actor)
            if actor.role not in {Role.ANALYST, Role.ADMIN} or document.plant_id not in actor.plants:
                raise DomainError("forbidden", "Current plant analyst/admin authorization is required", 403)
            existing = db.execute("SELECT * FROM document_heads WHERE document_id=?",
                                  (document.document_id,)).fetchone()
            if existing is not None:
                self._head(db, document.document_id, actor, active=False)
                if (existing["plant_id"] != document.plant_id or
                        set(json.loads(existing["shared_with"])) != set(document.shared_with)):
                    # Source updates cannot be used to bypass the admin-only ACL endpoint.
                    self._admin(actor, existing["plant_id"])
                    self._admin(actor, document.plant_id)
            elif db.execute("SELECT COUNT(*) FROM document_heads").fetchone()[0] >= MAX_DOCUMENTS:
                raise DomainError("document_limit", "Document registry is full", 413)
            previous = db.execute(
                "SELECT COALESCE(MAX(version),0) FROM document_versions WHERE document_id=?",
                (document.document_id,),
            ).fetchone()[0]
            if previous >= MAX_VERSIONS:
                raise DomainError("version_limit", "Document version retention limit reached", 413)
            count = db.execute("""
                SELECT COUNT(*) FROM document_chunks c JOIN document_heads h
                  ON h.document_id=c.document_id AND h.active_version=c.version
                WHERE c.document_id!=?
            """, (document.document_id,)).fetchone()[0]
            if count + len(chunks) > MAX_INDEX_CHUNKS:
                raise DomainError("index_limit", "Active chunk index is full", 413)
            # Authorization and bounds are established before any source reaches the provider.
            vectors, dimension = self._embed(
                [document.summaries[key] for key in SUMMARY_CHANNELS] + [c[2] for c in chunks],
                fingerprint,
            )
            self._current(db, actor)
            indexed_dimensions = {row[0] for row in db.execute("""
                SELECT DISTINCT v.dimension FROM document_versions v JOIN document_heads h
                  ON h.document_id=v.document_id AND h.active_version=v.version
                WHERE v.provider_fingerprint=? AND v.document_id!=?
            """, (fingerprint, document.document_id))}
            if indexed_dimensions and indexed_dimensions != {dimension}:
                raise DomainError("index_provider_mismatch",
                                  "Provider dimension disagrees with its existing active index", 409)
            version, content_hash = previous + 1, _sha(document.text)
            manifest = {"title": document.title, "plant_id": document.plant_id,
                        "text": document.text, "summaries": document.summaries}
            db.execute("""
                INSERT INTO document_heads VALUES(?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET active_version=excluded.active_version,
                  plant_id=excluded.plant_id,shared_with=excluded.shared_with
            """, (document.document_id, version, document.plant_id, canonical(document.shared_with)))
            db.execute("INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                document.document_id, version, document.title, document.plant_id, document.text,
                content_hash, canonical(document.summaries), digest(manifest), fingerprint, dimension,
                actor.actor_id, now(),
            ))
            db.executemany("INSERT INTO document_chunks VALUES(?,?,?,?,?,?)", [
                (document.document_id, version, ordinal, start, end, text)
                for ordinal, (start, end, text) in enumerate(chunks)
            ])
            rows = [(document.document_id, version, channel, -1, canonical(vectors[i]))
                    for i, channel in enumerate(SUMMARY_CHANNELS)]
            rows.extend((document.document_id, version, "chunk", i, canonical(vector))
                        for i, vector in enumerate(vectors[len(SUMMARY_CHANNELS):]))
            db.executemany("INSERT INTO document_vectors VALUES(?,?,?,?,?)", rows)
            revision = self._invalidate(db)
            self.store.audit(db, actor.actor_id, "document_ingest", {
                "document_id": document.document_id, "version": version,
                "content_hash": content_hash, "index_revision": revision,
                "provider_fingerprint": fingerprint,
            })
        return {"document_id": document.document_id, "version": version, "source_version": version,
                "content_hash": content_hash, "source_manifest_hash": digest(manifest),
                "chunks": len(chunks), "summary_embeddings": 5,
                "provider_fingerprint": fingerprint, "index_revision": revision}

    def delete(self, document_id: str, actor: Actor) -> dict:
        with self.store.transaction() as db:
            self._current(db, actor)
            row = self._head(db, document_id, actor)
            self._admin(actor, row["plant_id"])
            db.execute("UPDATE document_heads SET active_version=NULL WHERE document_id=?",
                       (document_id,))
            revision = self._invalidate(db)
            self.store.audit(db, actor.actor_id, "document_delete", {
                "document_id": document_id, "version": row["active_version"],
                "index_revision": revision,
            })
        return {"document_id": document_id, "deleted": True, "index_revision": revision}

    def set_acl(self, document_id: str, shared_with: list[str], actor: Actor) -> dict:
        if not isinstance(shared_with, list) or len(shared_with) > 100 or any(
            not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", x)
            for x in shared_with
        ) or len(shared_with) != len(set(shared_with)):
            raise DomainError("invalid_acl", "ACL must be a bounded list of unique actor IDs")
        with self.store.transaction() as db:
            self._current(db, actor)
            row = self._head(db, document_id, actor)
            self._admin(actor, row["plant_id"])
            db.execute("UPDATE document_heads SET shared_with=? WHERE document_id=?",
                       (canonical(shared_with), document_id))
            revision = self._invalidate(db)
            self.store.audit(db, actor.actor_id, "document_acl", {
                "document_id": document_id, "shared_with": shared_with, "index_revision": revision,
            })
        return {"document_id": document_id, "shared_with": shared_with, "index_revision": revision}

    @staticmethod
    def _check_source(row) -> dict[str, str]:
        try:
            summaries = json.loads(row["summaries_json"])
            manifest = {"title": row["title"], "plant_id": row["plant_id"],
                        "text": row["source_text"], "summaries": summaries}
            valid = (_sha(row["source_text"]) == row["content_hash"]
                     and digest(manifest) == row["source_manifest_hash"]
                     and set(summaries) == set(SUMMARY_CHANNELS))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise DomainError("document_integrity", "Source manifest is corrupt", 409) from exc
        if not valid:
            raise DomainError("document_integrity", "Source manifest hash mismatch", 409)
        return summaries

    def _load_index(self, db, scope: Scope, actor: Actor, fingerprint: str):
        # Crucially, no disallowed source, summary, or vector participates in ranking.
        heads = db.execute("""
            SELECT * FROM document_heads WHERE plant_id=? AND active_version IS NOT NULL
            ORDER BY document_id
        """, (scope.plant_id,)).fetchall()
        documents, chunks, dimensions = {}, {}, set()
        for head in heads:
            if not self._allowed(head, actor):
                continue
            key = head["document_id"]
            row = db.execute("SELECT * FROM document_versions WHERE document_id=? AND version=?",
                             (key, head["active_version"])).fetchone()
            if row is None:
                raise DomainError("index_integrity", "Active source version is missing", 409)
            if row["provider_fingerprint"] != fingerprint:
                raise DomainError("index_provider_mismatch",
                                  "Reingest accessible documents with the current local provider", 409)
            summaries = self._check_source(row)
            documents[key] = {"row": row, "summaries": summaries, "vectors": {}}
            dimensions.add(row["dimension"])
            stored = db.execute("""
                SELECT * FROM document_chunks WHERE document_id=? AND version=? ORDER BY ordinal
            """, (key, row["version"])).fetchall()
            expected = chunk_source(row["source_text"])
            if len(stored) != len(expected):
                raise DomainError("index_integrity", "Chunk index is incomplete", 409)
            for ordinal, (chunk, (start, end, text)) in enumerate(zip(stored, expected, strict=True)):
                if (chunk["ordinal"], chunk["quote_start"], chunk["quote_end"], chunk["text"]) != (
                    ordinal, start, end, text,
                ):
                    raise DomainError("document_integrity", "Chunk is not an exact source slice", 409)
                chunks[(key, ordinal)] = {"row": row, "chunk": chunk}
            vectors = db.execute("""
                SELECT * FROM document_vectors WHERE document_id=? AND version=?
            """, (key, row["version"])).fetchall()
            expected_keys = {(channel, -1) for channel in SUMMARY_CHANNELS}
            expected_keys.update(("chunk", ordinal) for ordinal in range(len(stored)))
            if {(v["channel"], v["ordinal"]) for v in vectors} != expected_keys:
                raise DomainError("index_integrity", "Five summary and all chunk vectors are required", 409)
            for vector in vectors:
                try:
                    normalized, _ = _vectors([json.loads(vector["vector_json"])], 1, row["dimension"])
                except (ValueError, TypeError, DomainError) as exc:
                    raise DomainError("index_integrity", "Stored vector is invalid", 409) from exc
                documents[key]["vectors"][(vector["channel"], vector["ordinal"])] = normalized[0]
        if len(documents) > MAX_DOCUMENTS or len(chunks) > MAX_INDEX_CHUNKS or len(dimensions) > 1:
            raise DomainError("index_integrity", "Index limits or embedding dimensions disagree", 409)
        return documents, chunks, next(iter(dimensions), None)

    def _rank(self, documents, chunks, query_vector, query: str, fingerprint: str):
        cosine_scores = {
            key: _cosine(query_vector, documents[key[0]]["vectors"][("chunk", key[1])])
            for key in chunks
        }
        chunk_order = sorted(chunks, key=lambda key: (-cosine_scores[key], key))
        rankings = {"chunk": chunk_order[:MAX_CANDIDATES]}
        per_document = defaultdict(list)
        for key in chunk_order:
            per_document[key[0]].append(key)
        for channel in SUMMARY_CHANNELS:
            ordered = sorted(documents, key=lambda key: (
                -_cosine(query_vector, documents[key]["vectors"][(channel, -1)]), key,
            ))
            # A summary nominates original chunks, never synthetic summary citations.
            rankings[channel] = [chunk for key in ordered for chunk in per_document[key][:2]][
                :MAX_CANDIDATES
            ]
        fused, channels = defaultdict(float), defaultdict(list)
        for channel, ranking in rankings.items():
            for rank, key in enumerate(ranking, start=1):
                fused[key] += 1.0 / (60 + rank)
                channels[key].append(channel)
        candidates = sorted(fused, key=lambda key: (-fused[key], key))[:MAX_CANDIDATES]
        try:
            raw = self.provider.rerank(query, [chunks[key]["chunk"]["text"] for key in candidates])
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError("provider_unavailable", "Local reranking provider failed", 503) from exc
        scores = _scores(raw, len(candidates))
        if _fingerprint(self.provider) != fingerprint:
            raise DomainError("provider_changed", "Provider changed during reranking", 409)
        scored = dict(zip(candidates, scores, strict=True))
        return [(key, scored[key], channels[key]) for key in sorted(
            candidates, key=lambda key: (-scored[key], -fused[key], key),
        )]

    @staticmethod
    def _chunk_id(row, chunk, revision: int, fingerprint: str) -> str:
        return "c_" + digest([row["document_id"], row["version"], row["content_hash"],
                              chunk["ordinal"], chunk["quote_start"], chunk["quote_end"],
                              revision, fingerprint])[:48]

    def _calibration(self, db, actor: Actor, scope: Scope, fingerprint: str, revision: int):
        row = db.execute("SELECT value FROM settings WHERE key='document_calibration'").fetchone()
        uncalibrated = "uncalibrated-local-" + _sha(fingerprint)[:16]
        if row is None:
            return None, EXPLORATORY_TOP_K, uncalibrated
        from .retrieval_calibration import CalibrationResult

        try:
            record = CalibrationResult.model_validate_json(row[0])
        except (ValueError, ValidationError) as exc:
            raise DomainError("calibration_integrity", "Calibration metadata is invalid", 409) from exc
        if (record.provider_fingerprint != fingerprint or record.index_revision != revision
                or record.actor_id != actor.actor_id or record.acl_version != actor.acl_version
                or scope.plant_id not in record.plant_ids):
            return None, EXPLORATORY_TOP_K, uncalibrated
        return record.threshold, record.top_k, record.calibration_version

    def search(self, query: str, scope: Scope, actor: Actor) -> RetrievalResult:
        return self._search(query, scope, actor)

    def _search(self, query: str, scope: Scope, actor: Actor,
                *, calibration_top_k: int | None = None) -> RetrievalResult:
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= MAX_QUERY_CHARS:
            raise DomainError("invalid_query", "Query must contain 2 to 2000 nonblank characters")
        if len(query) > MAX_QUERY_CHARS:
            raise DomainError("invalid_query", "Query exceeds the character limit")
        _source_text(query)
        scope = Scope.model_validate(scope.model_dump() if isinstance(scope, Scope) else scope)
        fingerprint = _fingerprint(self.provider)
        # This small bounded index uses a transaction to linearize ACL/revision checks,
        # ranking, and citation issuance. Mutations cannot race a provider response.
        with self.store.transaction() as db:
            self._current(db, actor)
            if scope.plant_id not in actor.plants:
                raise DomainError("forbidden", "Current plant authorization is required", 403)
            revision = self._revision(db)
            threshold, top_k, calibration_version = self._calibration(
                db, actor, scope, fingerprint, revision,
            )
            if calibration_top_k is not None:
                if not 1 <= calibration_top_k <= MAX_TOP_K:
                    raise DomainError("invalid_top_k", "Calibration top-k is out of bounds")
                threshold, top_k, calibration_version = None, calibration_top_k, "calibration-probe"
            documents, chunks, dimension = self._load_index(db, scope, actor, fingerprint)
            if not chunks:
                return RetrievalResult(hits=[], low_prior=True, index_revision=revision,
                                       calibration_version=calibration_version)
            vectors, actual_dimension = self._embed([query], fingerprint)
            if actual_dimension != dimension:
                raise DomainError("index_provider_mismatch", "Query embedding dimension changed", 409)
            ranked = self._rank(documents, chunks, vectors[0], query, fingerprint)
            self._current(db, actor)
            hits = []
            for key, score, channels in ranked:
                if threshold is not None and score < threshold:
                    continue
                row, chunk = chunks[key]["row"], chunks[key]["chunk"]
                hit = RetrievalHit(
                    document_id=row["document_id"], version=row["version"],
                    chunk_id=self._chunk_id(row, chunk, revision, fingerprint),
                    content_hash=row["content_hash"], text=chunk["text"],
                    quote_start=chunk["quote_start"], quote_end=chunk["quote_end"],
                    score=score, channels=channels,
                )
                hits.append(hit)
                payload = hit.model_dump(mode="json")
                issuance = (digest(payload), actor.actor_id, actor.acl_version, revision, fingerprint)
                # Reissue with a fresh id so retention cannot evict a hit in this response.
                # Match every authorization/snapshot binding; other issuances stay untouched.
                db.execute("""
                    DELETE FROM document_issued_hits
                    WHERE hit_hash=? AND actor_id=? AND acl_version=? AND index_revision=?
                      AND provider_fingerprint=?
                """, issuance)
                db.execute("""
                    INSERT INTO document_issued_hits(
                      hit_hash,actor_id,acl_version,index_revision,provider_fingerprint,
                      document_id,chunk_id,hit_json) VALUES(?,?,?,?,?,?,?,?)
                """, (*issuance, hit.document_id, hit.chunk_id, canonical(payload)))
                if len(hits) == top_k:
                    break
            db.execute("""
                DELETE FROM document_issued_hits WHERE id NOT IN
                  (SELECT id FROM document_issued_hits ORDER BY id DESC LIMIT ?)
            """, (MAX_ISSUED_HITS,))
            return RetrievalResult(hits=hits, low_prior=threshold is None or not hits,
                                   index_revision=revision, calibration_version=calibration_version)

    def _validate_hit(self, db, hit: RetrievalHit, actor: Actor):
        self._current(db, actor)
        fingerprint, revision = _fingerprint(self.provider), self._revision(db)
        issued = db.execute("""
            SELECT * FROM document_issued_hits
            WHERE hit_hash=? AND actor_id=? AND acl_version=? AND index_revision=?
              AND provider_fingerprint=?
        """, (digest(hit.model_dump(mode="json")), actor.actor_id, actor.acl_version,
              revision, fingerprint)).fetchone()
        if issued is None:
            raise DomainError("stale_citation", "Citation was not issued to this actor in the current index", 409)
        head = self._head(db, hit.document_id, actor)
        if hit.version != head["active_version"]:
            raise DomainError("stale_citation", "Citation is not from the active source version", 409)
        row = db.execute("SELECT * FROM document_versions WHERE document_id=? AND version=?",
                         (hit.document_id, hit.version)).fetchone()
        if row is None or row["provider_fingerprint"] != fingerprint:
            raise DomainError("stale_citation", "Citation source or provider changed", 409)
        self._check_source(row)
        if (hit.content_hash != row["content_hash"] or not
                0 <= hit.quote_start < hit.quote_end <= len(row["source_text"]) or
                row["source_text"][hit.quote_start:hit.quote_end] != hit.text):
            raise DomainError("citation_integrity", "Citation is not an exact source quotation", 409)
        chunk = db.execute("""
            SELECT * FROM document_chunks WHERE document_id=? AND version=?
              AND quote_start=? AND quote_end=? AND text=?
        """, (hit.document_id, hit.version, hit.quote_start, hit.quote_end, hit.text)).fetchone()
        if chunk is None or self._chunk_id(row, chunk, revision, fingerprint) != hit.chunk_id:
            raise DomainError("citation_integrity", "Citation chunk identity is invalid", 409)
        return row

    def validate_hit(self, hit: RetrievalHit | dict, actor: Actor) -> bool:
        """Return True only for an intact currently authorized issued hit; otherwise raise."""
        try:
            hit = RetrievalHit.model_validate(hit.model_dump() if isinstance(hit, RetrievalHit) else hit)
        except (ValidationError, TypeError, ValueError) as exc:
            raise DomainError("invalid_citation", "Citation does not match the retrieval schema") from exc
        with self.store.transaction() as db:
            self._validate_hit(db, hit, actor)
        return True

    def drill(self, document_id: str, chunk_id: str, actor: Actor) -> dict:
        """Enrich an issued hit with validated source metadata, not extra citation fields."""
        with self.store.transaction() as db:
            self._current(db, actor)
            self._head(db, document_id, actor)
            issued = db.execute("""
                SELECT hit_json FROM document_issued_hits
                WHERE actor_id=? AND acl_version=? AND document_id=? AND chunk_id=?
                  AND index_revision=? AND provider_fingerprint=? ORDER BY id DESC LIMIT 1
            """, (actor.actor_id, actor.acl_version, document_id, chunk_id,
                  self._revision(db), _fingerprint(self.provider))).fetchone()
            if issued is None:
                raise DomainError("stale_citation", "Drill requires a current, issued citation", 409)
            try:
                hit = RetrievalHit.model_validate_json(issued[0])
            except ValidationError as exc:
                raise DomainError("citation_integrity", "Stored citation is corrupt", 409) from exc
            row = self._validate_hit(db, hit, actor)
            return {**hit.model_dump(mode="json"), "title": row["title"], "plant_id": row["plant_id"],
                    "source_version": row["version"], "source_length": len(row["source_text"]),
                    "source_manifest_hash": row["source_manifest_hash"],
                    "provider_fingerprint": row["provider_fingerprint"],
                    "index_revision": self._revision(db),
                    "citations": [hit.model_dump(mode="json")]}

    def install_calibration(self, calibration, actor: Actor) -> dict:
        """Install measured local calibration; never silently apply it to a new snapshot."""
        from .retrieval_calibration import CalibrationResult

        record = CalibrationResult.model_validate(
            calibration.model_dump() if isinstance(calibration, CalibrationResult) else calibration
        )
        with self.store.transaction() as db:
            self._current(db, actor)
            for plant in record.plant_ids:
                self._admin(actor, plant)
            if (record.actor_id != actor.actor_id or record.acl_version != actor.acl_version
                    or record.index_revision != self._revision(db)
                    or record.provider_fingerprint != _fingerprint(self.provider)):
                raise DomainError("stale_calibration", "Calibration snapshot does not match", 409)
            payload = record.model_dump(mode="json")
            db.execute("INSERT INTO settings VALUES('document_calibration',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canonical(payload),))
            self.store.audit(db, actor.actor_id, "document_calibration", payload)
        return payload


def original_reports(plant_id: str = "P1") -> list[DocumentInput]:
    """Eight newly authored, explicitly fictional reports; never production evidence.

    The first four support calibration-fit labels and the latter four held-out
    evaluation labels. English and Chinese narratives and five human-written
    bilingual summaries are original fixtures, not excerpts of historical reports
    or model-generated summaries. Both languages describe the same fictional case.
    """
    records = [
        (
            "SYN-SEAL-01", "Synthetic: valve seal ridges after overnight storage",
            "Valve seals show crescent ridges after overnight storage, not an observed plant incident.",
            "Fictional plant P1, valve family VELA, bay Aster; a controlled 72-piece teaching exercise.",
            "Open storage may allow seal compound to absorb moisture; press speed is a competing cause.",
            "Ridges appeared on 9/24 open-tray seals, 1/24 sealed-tray seals, and 0/24 fresh controls.",
            "Keep trays sealed, record humidity, and randomize storage condition in a follow-up trial.",
            "ORIGINAL SYNTHETIC REPORT — not a production record.\n"
            "The Aster training cell assembled 72 fictional VELA valves. Operators noticed crescent "
            "ridges on the seal lip after components spent a night beside the wash station. Three "
            "groups contained 24 seals each: open trays had nine ridged seals, sealed trays had one, "
            "and freshly issued controls had none. These are invented counts for retrieval tests.\n"
            "The working hypothesis is moisture uptake during open storage. A faster press stroke "
            "was considered, but all three groups used the same stroke setting in this exercise. "
            "Assignment was not randomized, so the comparison does not prove causality. The proposed "
            "next step is a randomized storage trial with humidity logging and blind lip inspection. "
            "Until that trial, close seal trays and avoid declaring moisture a verified root cause.",
        ),
        (
            "SYN-TORQUE-02", "Synthetic: spindle torque drift following warm-up",
            "Fastener torque increases during a spindle's first half-hour after a cold start.",
            "Fictional ORBIT housing assembly; 36 joints sampled across three warm-up intervals.",
            "Thermal sensor zero drift, rather than a thread supplier change, could explain the pattern.",
            "Mean indicated torque was 7.8, 8.3, and 8.7 N m while reference-tool readings stayed near 8.0.",
            "Re-zero after a defined warm-up and compare against an independent reference tool.",
            "ORIGINAL SYNTHETIC REPORT — all identifiers and measurements are fictional.\n"
            "At the Juniper exercise bench, the ORBIT housing spindle displayed rising torque during "
            "the first thirty minutes of a cold start. Twelve joints were measured in each of three "
            "intervals. Indicated means were 7.8, 8.3, and 8.7 N m; a separate reference tool remained "
            "near 8.0 N m. A thread-lot explanation was considered, but the exercise used one lot.\n"
            "Sensor zero drift with temperature is a candidate explanation, not a confirmed fault. "
            "The reference tool had only a pre-run verification and may itself drift. A follow-up "
            "must log spindle temperature, verify both tools before and after the run, and alternate "
            "cold-start and warmed conditions. The temporary instruction is a documented warm-up "
            "and re-zero check, without increasing the tightening target to hide the discrepancy.",
        ),
        (
            "SYN-RINSE-03", "Synthetic: conductivity residue after shortened rinse",
            "A shortened final rinse coincides with elevated ionic residue on coated brackets.",
            "Fictional LUMEN bracket wash line, two 18-coupon rinse groups with equal bath chemistry.",
            "Insufficient rinse contact time may leave soluble salts; coating cure remains untested.",
            "Extract conductivity medians were 41 versus 13 microS/cm for short versus standard rinse.",
            "Restore the standard rinse and test randomized coupons with measured carryover volume.",
            "ORIGINAL SYNTHETIC REPORT — invented wash-process teaching data.\n"
            "The LUMEN bracket exercise compared a shortened final rinse with the standard cycle. "
            "Each group used eighteen coated coupons from the same fictional bath. Surface-extract "
            "conductivity medians were 41 microS/cm after the short rinse and 13 microS/cm after the "
            "standard rinse. Visual inspection alone did not reliably separate the groups.\n"
            "The provisional explanation is residual soluble salts from inadequate rinse contact. "
            "Drain angle and carryover volume were not logged and could confound this comparison. "
            "No claim about coating-cure chemistry follows from conductivity alone. Restore the "
            "standard rinse, randomize coupon order, measure carryover, and include blank extraction "
            "controls before deciding whether the rinse duration should be changed permanently.",
        ),
        (
            "SYN-LABEL-04", "Synthetic: barcode glare on curved cartons",
            "Glossy barcode labels fail optical reads mainly on sharply curved carton corners.",
            "Fictional NOVA kit packing study with forty glossy and forty matte trial labels.",
            "Specular glare at the scanner angle may dominate print density as a reading problem.",
            "Glossy corner labels had 11/40 first-pass misses; matte corner labels had 2/40 misses.",
            "Test label finish and scan angle independently; keep encoded content unchanged.",
            "ORIGINAL SYNTHETIC REPORT — this is an authored optical-reading scenario.\n"
            "NOVA training kits were packed with trial barcodes crossing a curved carton corner. "
            "Among forty glossy labels, eleven missed the first scan. Two of forty matte labels "
            "missed. All encoded the same fictional item identifier. The operator observed a bright "
            "reflection near the scanner, although illumination intensity was not measured.\n"
            "Specular glare is a candidate cause; label curvature and scanner distance also changed "
            "slightly during handling. The comparison is not proof that matte stock solves every "
            "barcode problem. A follow-up factorial exercise varies finish and scan angle separately "
            "while holding print density, content, distance, and carton shape constant. A temporary "
            "work instruction moves the code onto a flatter surface without altering its payload.",
        ),
        (
            "SYN-BRAZE-05", "Synthetic: braze voids after shielding-flow interruption",
            "Braze joints show isolated void clusters after a simulated shielding-gas interruption.",
            "Fictional PULSE heat-exchanger lab, twenty interrupted-flow and twenty stable-flow joints.",
            "Transient oxygen ingress may impair wetting; filler mass was held fixed but not reweighed.",
            "Radiographs flagged 6/20 interrupted-flow joints versus 1/20 stable-flow joints.",
            "Interlock low shielding flow and repeat blinded radiography with oxygen logging.",
            "ORIGINAL SYNTHETIC REPORT — fictional brazing exercise, not operational evidence.\n"
            "The PULSE heat-exchanger lab simulated a brief shielding-gas interruption during braze "
            "heating. Radiographs flagged clustered voids in six of twenty interrupted-flow joints "
            "and one of twenty stable-flow joints. The intended filler mass was identical, but no "
            "post-placement reweighing was done. These counts were created for this test corpus.\n"
            "Transient oxygen ingress and impaired wetting are plausible explanations, not verified "
            "causes. A separate filler-placement effect remains possible. Proposed work adds a "
            "low-flow interlock, synchronized oxygen logging, weighed filler, and blinded radiograph "
            "review. The exercise does not establish a production reject limit or justify releasing "
            "uninspected joints when gas-flow history is missing.",
        ),
        (
            "SYN-BEARING-06", "Synthetic: bearing whistle with grease overfill",
            "A high-frequency bearing whistle appears during a fictional overfill exercise.",
            "TERRA motor lab used three grease-fill levels and a fixed unloaded speed profile.",
            "Grease churning may produce the sound; bearing damage was not demonstrated.",
            "Whistle was recorded on 5/12 overfilled units, 1/12 nominal units, and 0/12 low-fill units.",
            "Weigh grease dispenses and repeat acoustic measurements under representative load.",
            "ORIGINAL SYNTHETIC REPORT — authored bearing-acoustics training scenario.\n"
            "The TERRA motor lab ran twelve units at each of three grease-fill levels using a fixed "
            "unloaded speed profile. A high-frequency whistle was heard on five overfilled units, "
            "one nominal-fill unit, and no low-fill units. Temperature rise was recorded informally "
            "and is not suitable for a quantitative comparison. No unit was a real customer return.\n"
            "Grease churning is the working hypothesis. The sound alone does not demonstrate bearing "
            "surface damage, and quieter low-fill units are not evidence of adequate lubrication. "
            "The proposed repeat weighs each grease dispense, randomizes test order, measures a "
            "calibrated acoustic spectrum, and applies representative load. The nominal fill remains "
            "the provisional instruction; lowering grease to suppress noise is not recommended.",
        ),
        (
            "SYN-ADHESIVE-07", "Synthetic: adhesive peel loss beyond open time",
            "Bonded panel peel strength decreases when assembly occurs beyond the adhesive open time.",
            "Fictional IRIS panel trial, sixteen prompt assemblies and sixteen delayed assemblies.",
            "Surface skin formation before mating may reduce adhesive wetting.",
            "Median peel loads were 92 N for prompt assembly and 57 N after the scripted delay.",
            "Time-stamp adhesive application and mating; repeat with controlled surface preparation.",
            "ORIGINAL SYNTHETIC REPORT — invented adhesive process comparison.\n"
            "The IRIS panel training trial compared sixteen promptly mated bonds with sixteen bonds "
            "assembled after a scripted delay beyond the nominal open time. Median peel loads were "
            "92 N and 57 N respectively. Peel testing used the same fixture, but surface preparation "
            "was documented only at batch level. The loads are synthetic teaching values.\n"
            "Skin formation before mating may reduce wetting and explain part of the peel loss. "
            "Surface contamination remains an alternative because individual cleaning records are "
            "absent. Add application and mating timestamps, record preparation per panel, and "
            "randomize the delay in a follow-up experiment. Do not convert this small comparison "
            "into a universal adhesive open-time limit or claim that delayed bonds always fail.",
        ),
        (
            "SYN-PROBE-08", "Synthetic: dimensional step after fixture probe replacement",
            "Reported pocket depth shifts after a fixture probe tip is replaced in a training run.",
            "Fictional HALO machined carriers, reference blocks checked before and after tip replacement.",
            "An unrefreshed probe offset could cause a metrology step rather than actual machining drift.",
            "The reference block shifted by +0.06 mm on the fixture but not on a separate comparator.",
            "Requalify the tip offset and remeasure retained samples before adjusting the cutting program.",
            "ORIGINAL SYNTHETIC REPORT — fictional dimensional-metrology investigation.\n"
            "During the HALO carrier exercise, reported pocket depth stepped upward after replacement "
            "of a fixture probe tip. A retained reference block read 0.06 mm higher on that fixture "
            "than before replacement. A separate comparator did not show the same step. No machine "
            "program edit was recorded during the exercise, though tool wear was not independently "
            "measured. The reference values are invented for retrieval evaluation.\n"
            "The leading candidate is an unrefreshed probe offset, not proven machining drift. "
            "Requalify the replacement tip, verify the reference block in both orientations, and "
            "remeasure retained carriers. Avoid changing the cutting program based solely on the "
            "shifted fixture reading. Independent dimensional evidence is required before assigning "
            "a root cause or dispositioning any real product.",
        ),
    ]
    chinese = [
        (
            "原创合成：阀门密封圈隔夜存放后唇边起棱",
            "阀门密封圈隔夜存放后出现月牙状唇边起棱；这是虚构现象，不是真实工厂事件。",
            "虚构VELA阀门教学试验共72件，比较敞口托盘、密封托盘与新领用对照。",
            "敞口存放吸湿可能导致密封材料变化；压装速度仍作为竞争解释考虑。",
            "每组24件，敞口组9件起棱，密封组1件，对照组0件；非随机分组不能证明因果。",
            "暂时密封托盘并记录湿度；后续随机安排存放条件，采用盲法检查唇边。",
            "原创合成报告，以下标识、计数与情景均为虚构，不得作为生产证据。\n"
            "阀门教学单元将72件VELA密封圈分为三组，每组24件。在清洗站旁隔夜存放后，"
            "敞口托盘组有9件出现月牙状唇边起棱，密封托盘组1件，新领用对照组0件。"
            "三组压装行程设置相同，因此这次比较优先考虑敞口吸湿，而不是更快的压装速度。\n"
            "试验没有随机分组，吸湿只能列为待验证假设，不能称为已确认根因。"
            "下一步应记录环境湿度，随机分配存放条件，并让不知道分组的检验员检查唇边。"
            "验证完成前保持托盘密封，不凭这些虚构计数处置任何真实产品。",
        ),
        (
            "原创合成：冷启动后主轴扭矩示值漂移",
            "冷启动后半小时内，主轴紧固扭矩示值逐渐升高，而独立参考工具读数基本稳定。",
            "虚构ORBIT壳体装配试验分三个热机时段，每时段测量12个接头，共36个。",
            "温升引起传感器零点漂移可能解释差异；同一螺纹批次不足以支持供应商变更假设。",
            "示值均值依次为7.8、8.3、8.7牛米；参考工具保持在约8.0牛米。",
            "规定热机后重新校零，记录温度，并在试验前后核查两套工具。",
            "原创合成报告，所有测量值均为教学虚构数据。\n"
            "ORBIT壳体装配台在主轴冷启动后的三十分钟内出现紧固扭矩示值上升。"
            "三个时段各测量12个接头，均值为7.8、8.3、8.7牛米；独立参考工具约为8.0牛米。"
            "试验使用同一螺纹批次，没有供应商切换的直接证据。\n"
            "候选解释是温升引起主轴传感器零点漂移，但参考工具只有试验前核查，自身漂移尚未排除。"
            "后续需交替安排冷机与热机条件，记录主轴温度，并对两套工具做前后核查。"
            "临时措施是热机后重新校零，不能通过提高紧固目标来掩盖读数差异。",
        ),
        (
            "原创合成：末道漂洗缩短后离子残留升高",
            "涂层支架末道漂洗时间缩短时，表面萃取液电导率及离子残留升高。",
            "虚构LUMEN清洗线采用相同槽液，每组18片试片，比较短漂洗与标准漂洗。",
            "漂洗接触时间不足可能留下可溶盐；涂层固化机理尚未验证。",
            "短漂洗与标准漂洗的电导率中位数分别为41和13微西门子每厘米。",
            "恢复标准漂洗，随机排列试片，测量带出液量，并加入空白萃取对照。",
            "原创合成报告，清洗工艺与数值均为虚构。\n"
            "LUMEN支架教学试验使用相同槽液，对短末道漂洗和标准漂洗各安排18片涂层试片。"
            "表面萃取液电导率中位数分别为41和13微西门子每厘米；单靠目视不能可靠区分两组。\n"
            "漂洗接触不足导致可溶盐残留是候选解释。沥液角度与带出液量没有记录，仍可能构成混杂。"
            "电导率比较不能证明涂层固化异常。临时恢复标准漂洗，后续随机排列试片，"
            "记录带出液量并增加空白萃取对照，再讨论是否永久修改漂洗时间。",
        ),
        (
            "原创合成：弯曲纸箱角部条码反光漏读",
            "亮面条码贴在弯曲纸箱角部时，首次扫描漏读较多，扫描角度处可见反光。",
            "虚构NOVA包装练习分别使用40张亮面与40张哑光标签，编码内容相同。",
            "镜面反光可能影响读取；曲率、扫描距离和印刷密度需分别控制。",
            "亮面角部标签首次漏读11/40，哑光角部标签漏读2/40。",
            "独立改变标签表面与扫描角度；临时移至较平面区域，保持编码内容不变。",
            "原创合成报告，条码测试不对应真实订单或客户投诉。\n"
            "NOVA包装教学中，标签跨过弯曲纸箱角部。40张亮面标签有11张首次扫描漏读，"
            "40张哑光标签有2张漏读，所有标签编码相同。操作者观察到扫描器附近有强反光，"
            "但没有测量照度。\n"
            "镜面反光只是候选原因，拿取时标签曲率与扫描距离也有小幅变化。"
            "不能据此宣称哑光标签可解决所有条码问题。下一轮分别改变表面光泽与扫描角度，"
            "固定印刷密度、距离、编码和纸箱形状；临时把条码移到较平区域，不改变载荷内容。",
        ),
        (
            "原创合成：保护气短时中断后钎焊孔洞",
            "模拟保护气流量短时中断后，钎焊接头射线片出现局部孔洞簇。",
            "虚构PULSE换热器试验设中断气流与稳定气流两组，各20个接头。",
            "氧气瞬时进入可能削弱润湿；钎料放置量虽设定相同但没有复称。",
            "射线检查发现中断组6/20接头有孔洞簇，稳定组1/20。",
            "增加保护气低流量联锁，同步记录氧含量、复称钎料，并盲审射线片。",
            "原创合成报告，钎焊情景和检验计数均为虚构。\n"
            "PULSE换热器教学试验在钎焊加热期间模拟短暂保护气中断。两组各20个接头，"
            "射线检查标记中断组6个、稳定气流组1个存在孔洞簇。两组钎料目标质量相同，"
            "但放置后未复称，因此仍不能排除钎料放置差异。\n"
            "氧气进入并影响润湿是一项候选解释，而非已证实根因。建议增加低流量联锁，"
            "同步记录氧含量、称量钎料，并让盲审人员复核射线片。本例不能用来建立生产报废线，"
            "也不能支持在保护气历史缺失时放行未经检查的真实接头。",
        ),
        (
            "原创合成：润滑脂过量与轴承啸叫",
            "虚构电机试验在润滑脂过量加注时出现较多高频轴承啸叫。",
            "TERRA教学电机采用三种加脂量，每组12台，执行固定空载转速程序。",
            "润滑脂搅动可能产生声音；仅凭啸叫不能证明轴承表面损伤。",
            "过量组5/12、标称组1/12、少量组0/12出现啸叫；温升记录不完整。",
            "称量每次加脂量，在代表性负载下测量校准声谱，不应为降噪而任意减脂。",
            "原创合成报告，所有电机均为虚构教学对象，并非真实客退。\n"
            "TERRA电机练习将加脂量设为过量、标称、少量三档，每档12台，使用固定空载转速程序。"
            "高频轴承啸叫分别出现于5台、1台、0台。温升只有非正式记录，不能作定量比较。\n"
            "润滑脂搅动是工作假设，啸叫本身不能证明轴承表面损伤，少脂组安静也不能证明润滑充分。"
            "下一轮需逐台称量加脂量、随机安排顺序、测量校准声谱，并施加代表性负载。"
            "临时维持标称加脂量，不建议单纯减少润滑脂来压低噪声。",
        ),
        (
            "原创合成：胶粘剂晾置超时后剥离载荷下降",
            "涂胶后超过允许晾置时间才合拢的面板，在教学试验中剥离强度较低。",
            "虚构IRIS面板试验比较及时合拢与延迟合拢，各16件，使用相同剥离夹具。",
            "合拢前胶面结皮可能降低润湿；单件清洁记录缺失，仍需排除表面污染。",
            "及时组剥离载荷中位数92牛，延迟组57牛；这些数值不是验收标准。",
            "记录涂胶与合拢时间，逐件记录表面处理，并随机安排延迟条件。",
            "原创合成报告，胶粘工艺比较与载荷均为虚构。\n"
            "IRIS面板练习分别准备16件及时合拢接头和16件超过标称晾置时间才合拢的接头。"
            "使用同一剥离夹具时，剥离载荷中位数为92牛和57牛。表面清洁仅有批次记录，"
            "没有逐件记录，因此不能排除污染差异。\n"
            "胶面在合拢前结皮、降低润湿是候选解释，不是确定结论。"
            "后续需记录涂胶时间与合拢时间，逐件记录表面处理，并随机安排延迟条件。"
            "不能把这个小样本比较变成通用晾置时限，也不能宣称所有延迟粘接件都会失效。",
        ),
        (
            "原创合成：测头换针后槽深读数阶跃",
            "夹具测头更换针尖后，槽深示值发生阶跃，而独立比较仪没有同样变化。",
            "虚构HALO加工载体试验在换针前后检查保留的参考块；未记录切削程序修改。",
            "更换针尖后未更新测头偏置可能引起计量变化，而不是真实加工漂移。",
            "同一参考块在夹具上的读数增加0.06毫米，独立比较仪未见对应阶跃。",
            "重新标定针尖偏置，双向核查参考块并复测留样，再决定是否调整切削程序。",
            "原创合成报告，尺寸值与加工场景均为虚构。\n"
            "HALO载体教学中，更换夹具测头针尖后，槽深示值出现上跳。"
            "保留参考块在该夹具上的读数比更换前高0.06毫米，独立比较仪没有同样阶跃。"
            "期间没有记录切削程序修改，但刀具磨损也未经独立测量。\n"
            "优先候选解释是换针后未刷新测头偏置，而非已证实加工漂移。"
            "需重新标定针尖、从两个方向核查参考块，并复测保留载体。"
            "不要只依据夹具示值变化就修改切削程序；认定根因和处置真实产品都需要独立尺寸证据。",
        ),
    ]
    return [DocumentInput(
        document_id=record[0], title=zh[0] + " / " + record[1], plant_id=plant_id,
        summaries={key: zh[i + 1] + "\n" + record[i + 2]
                   for i, key in enumerate(SUMMARY_CHANNELS)},
        text=zh[6] + "\n\n" + record[7],
    ) for record, zh in zip(records, chinese, strict=True)]
