"""Tests use an explicitly synthetic lexical double, NOT BGE or model evaluation."""
from __future__ import annotations

import hashlib
import io
import math
import re

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from quality_analysis import documents as module
from quality_analysis.documents import DocumentStore, chunk_source, original_reports, parse_upload
from quality_analysis.retrieval_calibration import (
    calibrate,
    original_calibration_cases,
    original_heldout_cases,
)
from quality_analysis.schemas import Actor, DomainError, RetrievalHit, Scope
from quality_analysis.storage import Store


class SyntheticLexicalProvider:
    """Deterministic hashed lexical features for tests only; never an embedding model."""

    fingerprint = "synthetic-lexical-unit-test-v1"

    def __init__(self):
        self.embedded = []
        self.reranked = []

    @staticmethod
    def tokens(text):
        return re.findall(r"[a-z0-9]+", text.lower())

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        vectors = []
        for text in texts:
            vector = [0.0] * 128
            for token in self.tokens(text) or [text]:
                slot = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % len(vector)
                vector[slot] += 1.0
            vectors.append(vector)
        return vectors

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.reranked.append((query, list(texts)))
        query_tokens = set(self.tokens(query))
        return [len(query_tokens & set(self.tokens(text))) / math.sqrt(
            max(1, len(query_tokens) * len(set(self.tokens(text))))
        ) for text in texts]


class SyntheticChineseLexicalProvider(SyntheticLexicalProvider):
    """Chinese character-bigram lexical double; NOT multilingual model evaluation."""

    fingerprint = "synthetic-lexical-chinese-bigrams-unit-test-v1"

    @staticmethod
    def tokens(text):
        tokens = SyntheticLexicalProvider.tokens(text)
        for run in re.findall(r"[㐀-鿿]+", text):
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        return tokens


@pytest.fixture
def environment(tmp_path):
    store = Store(tmp_path)
    actors = {}
    for name, role, plants in [
        ("admin", "admin", ["P1"]), ("reader", "analyst", ["P1"]),
        ("other", "analyst", ["P2"]), ("admin2", "admin", ["P2"]),
        ("operator", "operator", ["P1"]),
    ]:
        actors[name] = store.authenticate(store.provision_user(name, role, plants))
    provider = SyntheticLexicalProvider()
    documents = DocumentStore(store, provider)
    scope = Scope(plant_id="P1", product_id="SYNTHETIC", start_date="2025-01-01",
                  end_date="2025-02-01")
    return store, documents, provider, actors, scope


def pdf_bytes(*, text: str | None = None, encrypted=False, image=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text is not None:
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if image:
        raster = DecodedStreamObject()
        raster.set_data(b"\xff\xff\xff")
        raster.update({NameObject("/Type"): NameObject("/XObject"),
                       NameObject("/Subtype"): NameObject("/Image"),
                       NameObject("/Width"): NumberObject(1),
                       NameObject("/Height"): NumberObject(1),
                       NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                       NameObject("/BitsPerComponent"): NumberObject(8)})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(raster)})
        })
        stream = DecodedStreamObject()
        stream.set_data(b"q 100 0 0 100 0 0 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("not-a-real-secret")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_parse_preserves_exact_utf8_source_and_unicode_offsets():
    source = "﻿原创合成报告\r\n\n  Seal ridges are fictional.\tDo not normalize.\n"
    assert parse_upload("report.MD", source.encode()) == source
    assert parse_upload("report.txt", source.encode()) == source
    chunks = chunk_source(source * 30)
    assert chunks == chunk_source(source * 30)
    assert chunks[0][0] == 0 and chunks[-1][1] == len(source * 30)
    for start, end, text in chunks:
        assert (source * 30)[start:end] == text
    for before, after in zip(chunks, chunks[1:], strict=False):
        assert before[1] - after[0] == module.CHUNK_OVERLAP


@pytest.mark.parametrize(("name", "content", "code"), [
    ("x.docx", b"not a docx", "unsupported_upload"),
    ("x.txt", b"", "empty_document"),
    ("x.md", b"  \r\n", "empty_document"),
    ("x.txt", b"\xff\xfe", "invalid_encoding"),
    ("x.txt", b"binary\x00file", "invalid_text"),
    ("x.pdf", b"not a PDF", "invalid_pdf"),
])
def test_parse_rejects_invalid_uploads(name, content, code):
    with pytest.raises(DomainError) as error:
        parse_upload(name, content)
    assert error.value.code == code


def test_real_pdf_parser_and_no_ocr_or_decryption():
    text = "Original synthetic PDF: probe offset changed after tip replacement."
    assert text in parse_upload("probe.pdf", pdf_bytes(text=text))
    for content, code in [
        (pdf_bytes(), "empty_document"),
        (pdf_bytes(image=True), "empty_document"),
        (pdf_bytes(text=text, encrypted=True), "encrypted_pdf"),
    ]:
        with pytest.raises(DomainError) as error:
            parse_upload("report.pdf", content)
        assert error.value.code == code


def test_upload_bounds(monkeypatch):
    monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 20)
    with pytest.raises(DomainError, match="byte limit"):
        parse_upload("x.txt", b"x" * 21)
    monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(module, "MAX_SOURCE_CHARS", 10)
    with pytest.raises(DomainError, match="character limit"):
        parse_upload("x.md", b"x" * 11)


def test_original_reports_and_five_embedding_channels(environment):
    store, documents, provider, actors, scope = environment
    reports = original_reports()
    assert len(reports) == 8
    assert len({r.document_id for r in reports}) == len(reports)
    assert all("ORIGINAL SYNTHETIC REPORT" in r.text for r in reports)
    report = reports[0]
    result = documents.ingest(report, actors["admin"])
    assert result["version"] == result["source_version"] == 1
    assert result["content_hash"] == hashlib.sha256(report.text.encode()).hexdigest()
    assert result["summary_embeddings"] == 5
    assert provider.embedded[:5] == [report.summaries[key] for key in module.SUMMARY_CHANNELS]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM document_vectors").fetchone()[0] == (
            5 + result["chunks"]
        )
    response = documents.search("valve seal ridges overnight trays", scope, actors["reader"])
    assert response.low_prior is True
    assert response.calibration_version.startswith("uncalibrated-local-")
    assert response.hits
    assert len({hit.chunk_id for hit in response.hits}) == len(response.hits)
    for hit in response.hits:
        assert report.text[hit.quote_start:hit.quote_end] == hit.text
        assert set(hit.channels) == {*module.SUMMARY_CHANNELS, "chunk"}
        assert documents.validate_hit(hit, actors["reader"]) is True
        drilled = documents.drill(hit.document_id, hit.chunk_id, actors["reader"])
        assert drilled["text"] == hit.text
        assert drilled["citations"] == [hit.model_dump(mode="json")]
        assert drilled["content_hash"] == result["content_hash"]
        assert drilled["index_revision"] == response.index_revision
    assert store.revisions()[1] == result["index_revision"]


def test_acl_filters_before_ranking_and_never_exposes_private_summaries(environment):
    _, documents, provider, actors, scope = environment
    public, private, foreign = original_reports()[:3]
    documents.ingest(public, actors["admin"])
    first = documents.search("seal ridges", scope, actors["reader"])
    private = private.model_copy(update={"shared_with": ["admin"]})
    foreign = foreign.model_copy(update={"plant_id": "P2"})
    documents.ingest(private, actors["admin"])
    documents.ingest(foreign, actors["admin2"])
    provider.embedded.clear()
    provider.reranked.clear()
    second = documents.search("seal ridges", scope, actors["reader"])
    assert [(h.document_id, h.score) for h in first.hits] == [
        (h.document_id, h.score) for h in second.hits
    ]
    assert provider.embedded == ["seal ridges"]
    assert provider.reranked
    assert all(text in public.text for _, texts in provider.reranked for text in texts)
    assert not any(h.document_id in {private.document_id, foreign.document_id} for h in second.hits)
    with pytest.raises(DomainError, match="unavailable"):
        documents.drill(private.document_id, "c_unknown", actors["reader"])
    with pytest.raises(DomainError) as error:
        documents.search("seal ridges", scope, actors["other"])
    assert error.value.status == 403


def test_current_authorization_not_forged_or_stale_actor(environment):
    store, documents, provider, actors, scope = environment
    documents.ingest(original_reports()[0], actors["admin"])
    hit = documents.search("seal ridges", scope, actors["reader"]).hits[0]
    forged = actors["reader"].model_copy(update={"role": "admin"})
    with pytest.raises(DomainError) as error:
        documents.delete(hit.document_id, forged)
    assert error.value.code == "acl_changed"
    with store.transaction() as db:
        db.execute("UPDATE users SET acl_version=acl_version+1 WHERE actor_id='reader'")
    count = len(provider.embedded)
    with pytest.raises(DomainError, match="Authorization changed"):
        documents.search("seal ridges", scope, actors["reader"])
    with pytest.raises(DomainError):
        documents.validate_hit(hit, actors["reader"])
    assert len(provider.embedded) == count
    refreshed = Actor(actor_id="reader", role="analyst", plants=["P1"], acl_version=2)
    with pytest.raises(DomainError, match="not issued"):
        documents.validate_hit(hit, refreshed)
    with store.transaction() as db:
        db.execute("UPDATE users SET active=0 WHERE actor_id='reader'")
    with pytest.raises(DomainError) as error:
        documents.search("seal ridges", scope, refreshed)
    assert error.value.status == 401


def test_mutations_require_admin_and_current_document_access(environment):
    _, documents, _, actors, _ = environment
    report = original_reports()[0]
    for who in ("operator", "admin2"):
        with pytest.raises(DomainError):
            documents.ingest(report, actors[who])
    documents.ingest(report, actors["reader"])
    assert documents.ingest(report, actors["reader"])["version"] == 2
    with pytest.raises(DomainError):
        documents.ingest(report.model_copy(update={"shared_with": ["reader"]}), actors["reader"])
    for who in ("reader", "operator", "admin2"):
        with pytest.raises(DomainError):
            documents.delete(report.document_id, actors[who])
        with pytest.raises(DomainError):
            documents.set_acl(report.document_id, [], actors[who])
    documents.set_acl(report.document_id, ["reader"], actors["admin"])
    # An administrator cannot bypass a document's explicit sharing allowlist.
    with pytest.raises(DomainError):
        documents.ingest(report, actors["admin"])
    with pytest.raises(DomainError):
        documents.set_acl(report.document_id, [], actors["admin"])


@pytest.mark.parametrize("mutation", ["update", "delete", "acl", "unrelated"])
def test_global_revision_invalidates_all_prior_citations(environment, mutation):
    store, documents, _, actors, scope = environment
    first, second = original_reports()[:2]
    documents.ingest(first, actors["admin"])
    documents.ingest(second, actors["admin"])
    hit = documents.search("valve seal ridges overnight", scope, actors["reader"]).hits[0]
    before = store.revisions()[1]
    if mutation == "update":
        documents.ingest(first.model_copy(update={"text": first.text + "\nA new original note."}),
                         actors["admin"])
    elif mutation == "delete":
        documents.delete(first.document_id, actors["admin"])
    elif mutation == "acl":
        documents.set_acl(first.document_id, ["admin"], actors["admin"])
    else:
        documents.delete(second.document_id, actors["admin"])
    assert store.revisions()[1] == before + 1
    with pytest.raises(DomainError):
        documents.validate_hit(hit, actors["reader"])
    with pytest.raises(DomainError):
        documents.drill(hit.document_id, hit.chunk_id, actors["reader"])


def test_versions_are_immutable_current_pointer_and_reingest_after_delete(environment):
    store, documents, _, actors, scope = environment
    report = original_reports()[0]
    original = documents.ingest(report, actors["admin"])
    updated_report = report.model_copy(update={"text": report.text + "\nRevised trial planned."})
    updated = documents.ingest(updated_report, actors["admin"])
    assert updated["version"] == 2
    assert updated["content_hash"] != original["content_hash"]
    assert all(h.version == 2 for h in documents.search("seal ridges", scope, actors["reader"]).hits)
    documents.delete(report.document_id, actors["admin"])
    assert documents.search("seal ridges", scope, actors["reader"]).hits == []
    third = documents.ingest(report, actors["admin"])
    assert third["version"] == 3
    with store.connect() as db:
        rows = db.execute("SELECT * FROM document_versions ORDER BY version").fetchall()
        assert [row["source_text"] for row in rows] == [report.text, updated_report.text, report.text]
        assert rows[0]["content_hash"] == original["content_hash"]


@pytest.mark.parametrize("field,value", [
    ("text", "fabricated quotation"), ("quote_start", 1), ("quote_end", 2),
    ("version", 99), ("content_hash", "0" * 64), ("chunk_id", "c_invented"),
    ("score", 1000.0), ("channels", ["made-up"]),
])
def test_no_forged_or_modified_citations(environment, field, value):
    _, documents, _, actors, scope = environment
    documents.ingest(original_reports()[0], actors["admin"])
    hit = documents.search("seal ridges", scope, actors["reader"]).hits[0]
    forged = hit.model_dump()
    forged[field] = value
    with pytest.raises(DomainError):
        documents.validate_hit(forged, actors["reader"])
    with pytest.raises(DomainError):
        documents.validate_hit(hit, actors["operator"])
    with pytest.raises(DomainError):
        documents.drill(hit.document_id, hit.chunk_id, actors["operator"])
    with pytest.raises(DomainError):
        documents.validate_hit({"document_id": hit.document_id}, actors["reader"])


def test_source_and_chunk_corruption_fail_closed(environment):
    store, documents, _, actors, scope = environment
    report = original_reports()[0]
    documents.ingest(report, actors["admin"])
    hit = documents.search("seal ridges", scope, actors["reader"]).hits[0]
    with store.transaction() as db:
        db.execute("UPDATE document_versions SET source_text=source_text || 'tampered'")
    with pytest.raises(DomainError, match="hash mismatch"):
        documents.validate_hit(hit, actors["reader"])
    with pytest.raises(DomainError, match="hash mismatch"):
        documents.search("seal ridges", scope, actors["reader"])
    with store.transaction() as db:
        db.execute("UPDATE document_versions SET source_text=?", (report.text,))
        db.execute("UPDATE document_chunks SET text='fabricated'")
    with pytest.raises(DomainError):
        documents.validate_hit(hit, actors["reader"])
    with pytest.raises(DomainError, match="exact source slice"):
        documents.search("seal ridges", scope, actors["reader"])


def test_provider_fingerprint_never_silently_reuses_stale_vectors(environment):
    _, documents, provider, actors, scope = environment
    report = original_reports()[0]
    documents.ingest(report, actors["admin"])
    hit = documents.search("seal ridges", scope, actors["reader"]).hits[0]
    provider.fingerprint = "synthetic-lexical-unit-test-v2"
    with pytest.raises(DomainError) as error:
        documents.search("seal ridges", scope, actors["reader"])
    assert error.value.code == "index_provider_mismatch"
    with pytest.raises(DomainError):
        documents.validate_hit(hit, actors["reader"])
    documents.ingest(report, actors["admin"])
    assert documents.search("seal ridges", scope, actors["reader"]).hits[0].version == 2


@pytest.mark.parametrize("bad", [
    [], [[0.0, 0.0]], [[float("nan"), 1.0]], [[float("inf"), 1.0]], [[True, 1.0]],
    [["1", 2.0]], [[1.0], [1.0, 2.0]],
])
def test_invalid_embeddings_roll_back_without_revision(environment, bad, monkeypatch):
    store, documents, provider, actors, _ = environment
    monkeypatch.setattr(provider, "embed", lambda texts: bad)
    with pytest.raises(DomainError):
        documents.ingest(original_reports()[0], actors["admin"])
    assert store.revisions()[1] == 0
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0] == 0


def test_embedding_values_and_dimensions_checked_after_correct_batch_count(environment, monkeypatch):
    store, documents, provider, actors, scope = environment
    report = original_reports()[0]
    for vector in ([0.0, 0.0], [1.0, float("nan")], [1.0, float("inf")], [True, 1.0],
                   [10 ** 1000, 1.0], [1.0] * (module.MAX_EMBEDDING_DIM + 1)):
        monkeypatch.setattr(provider, "embed", lambda texts, v=vector: [v for _ in texts])
        with pytest.raises(DomainError) as error:
            documents.ingest(report, actors["admin"])
        assert error.value.code == "invalid_embedding"
    monkeypatch.setattr(provider, "embed", lambda texts: [[1.0, 0.0] for _ in texts])
    documents.ingest(report, actors["admin"])
    monkeypatch.setattr(provider, "embed", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    with pytest.raises(DomainError, match="existing active index"):
        documents.ingest(original_reports()[1], actors["admin"])
    with pytest.raises(DomainError) as error:
        documents.search("seal ridges", scope, actors["reader"])
    assert error.value.code == "index_provider_mismatch"
    with store.transaction() as db:
        db.execute("UPDATE document_vectors SET vector_json='[0,0]'")
    with pytest.raises(DomainError) as error:
        documents.search("seal ridges", scope, actors["reader"])
    assert error.value.code == "index_integrity"


@pytest.mark.parametrize("bad", [[], [float("nan")], [float("inf")], [True]])
def test_invalid_rerank_scores_are_not_returned(environment, monkeypatch, bad):
    _, documents, provider, actors, scope = environment
    documents.ingest(original_reports()[0], actors["admin"])
    monkeypatch.setattr(provider, "rerank", lambda query, texts: bad * len(texts))
    with pytest.raises(DomainError) as error:
        documents.search("seal ridges", scope, actors["reader"])
    assert error.value.code == "invalid_rerank"


def test_provider_failure_and_mid_call_fingerprint_change_no_fallback(environment, monkeypatch):
    store, documents, provider, actors, scope = environment

    def fail(_):
        raise ConnectionError("local provider offline")

    monkeypatch.setattr(provider, "embed", fail)
    with pytest.raises(DomainError) as error:
        documents.ingest(original_reports()[0], actors["admin"])
    assert error.value.code == "provider_unavailable"
    assert store.revisions()[1] == 0

    def change(texts):
        provider.fingerprint += "-changed"
        return [[1.0, 2.0] for _ in texts]

    monkeypatch.setattr(provider, "embed", change)
    with pytest.raises(DomainError) as error:
        documents.ingest(original_reports()[0], actors["admin"])
    assert error.value.code == "provider_changed"
    assert store.revisions()[1] == 0


def test_bounds_and_empty_index(environment, monkeypatch):
    _, documents, provider, actors, scope = environment
    result = documents.search("unknown question", scope, actors["reader"])
    assert result.hits == [] and result.low_prior
    assert provider.embedded == [] and provider.reranked == []
    for query in ("", " ", "a", "x" * (module.MAX_QUERY_CHARS + 1)):
        with pytest.raises(DomainError):
            documents.search(query, scope, actors["reader"])
    first, second = original_reports()[:2]
    documents.ingest(first, actors["admin"])
    monkeypatch.setattr(module, "MAX_DOCUMENTS", 1)
    with pytest.raises(DomainError, match="registry is full"):
        documents.ingest(second, actors["admin"])
    monkeypatch.setattr(module, "MAX_VERSIONS", 1)
    with pytest.raises(DomainError, match="retention limit"):
        documents.ingest(first, actors["admin"])
    monkeypatch.setattr(module, "MAX_CHUNKS_PER_DOCUMENT", 1)
    with pytest.raises(DomainError, match="chunk limit"):
        chunk_source("x" * (module.CHUNK_SIZE + 1))


def test_actual_cosine_rrf_dedup_and_reranker_changes_final_order(environment, monkeypatch):
    _, documents, provider, actors, scope = environment
    first, second = original_reports()[:2]
    first = first.model_copy(update={"text": "seal " * 30})
    second = second.model_copy(update={"text": "torque " * 30})
    documents.ingest(first, actors["admin"])
    documents.ingest(second, actors["admin"])
    assert module._cosine([1.0, 0.0], [1.0, 0.0]) == 1
    assert module._cosine([1.0, 0.0], [0.0, 1.0]) == 0
    assert module._cosine([1.0, 0.0], [-1.0, 0.0]) == -1
    response = documents.search("seal", scope, actors["reader"])
    assert response.hits[0].document_id == first.document_id
    monkeypatch.setattr(provider, "rerank", lambda query, texts: [
        5.0 if "torque" in text else -5.0 for text in texts
    ])
    response = documents.search("seal", scope, actors["reader"])
    assert response.hits[0].document_id == second.document_id
    assert len(response.hits) == len({hit.chunk_id for hit in response.hits}) == 2
    assert all(len(hit.channels) == 6 for hit in response.hits)


def test_calibration_measures_synthetic_double_and_installs_explicitly(environment):
    store, documents, _, actors, scope = environment
    for report in original_reports():
        documents.ingest(report, actors["admin"])
    cases = original_calibration_cases()
    assert original_heldout_cases() == [case for case in cases if case.split == "eval"]
    assert documents.search(cases[0].query, scope, actors["admin"]).low_prior is True
    record = calibrate(documents, cases, actors["admin"], evaluator_kind="synthetic-lexical",
                       dataset_kind="original-synthetic")
    assert record.production_validated is False
    assert record.evaluator_kind == "synthetic-lexical"
    assert record.provider_fingerprint.startswith("synthetic-lexical")
    assert set(record.fit_case_ids).isdisjoint(record.eval_case_ids)
    assert record.fit_metrics.case_count == record.eval_metrics.case_count == 6
    assert record.fit_metrics.negative_case_count == record.eval_metrics.negative_case_count == 2
    assert record.index_revision == store.revisions()[1]
    assert record.top_k in (1, 2, 4, 8)
    assert math.isfinite(record.threshold)
    # Merely measuring never installs or invents a production threshold.
    assert documents.search(cases[0].query, scope, actors["admin"]).low_prior is True
    documents.install_calibration(record, actors["admin"])
    response = documents.search(cases[0].query, scope, actors["admin"])
    assert response.calibration_version == record.calibration_version
    assert response.low_prior is (not response.hits)
    assert all(hit.score >= record.threshold for hit in response.hits)
    assert len(response.hits) <= record.top_k
    # A different ACL cohort cannot inherit this calibration.
    assert documents.search(cases[0].query, scope, actors["reader"]).low_prior is True
    documents.ingest(original_reports()[0], actors["admin"])
    assert documents.search(cases[0].query, scope, actors["admin"]).low_prior is True
    with pytest.raises(DomainError, match="snapshot"):
        documents.install_calibration(record, actors["admin"])


def test_heldout_labels_cannot_influence_fit_selection(environment):
    _, documents, _, actors, _ = environment
    for report in original_reports():
        documents.ingest(report, actors["admin"])
    cases = original_calibration_cases()
    first = calibrate(documents, cases, actors["admin"], evaluator_kind="synthetic-lexical")
    changed = [case.model_copy(update={"relevant_document_ids": ["SYN-PROBE-08"]})
               if case.split == "eval" and case.relevant_document_ids else case for case in cases]
    second = calibrate(documents, changed, actors["admin"], evaluator_kind="synthetic-lexical")
    assert (first.threshold, first.top_k, first.fit_metrics) == (
        second.threshold, second.top_k, second.fit_metrics
    )
    assert first.eval_dataset_hash != second.eval_dataset_hash


@pytest.mark.parametrize("problem", ["same_query", "same_document", "no_negatives", "bge_claim"])
def test_calibration_rejects_leakage_missing_negatives_or_false_model_claim(environment, problem):
    _, documents, _, actors, _ = environment
    cases = original_calibration_cases()
    kind = "synthetic-lexical"
    if problem == "same_query":
        cases[-1] = cases[-1].model_copy(update={"query": "  " + cases[0].query.upper() + " "})
    elif problem == "same_document":
        cases[-1] = cases[-1].model_copy(update={"relevant_document_ids": ["SYN-SEAL-01"]})
    elif problem == "no_negatives":
        cases = [case for case in cases if case.relevant_document_ids]
    else:
        kind = "bge-local"
    with pytest.raises(DomainError) as error:
        calibrate(documents, cases, actors["admin"], evaluator_kind=kind)
    assert error.value.code in {"calibration_leakage", "invalid_calibration", "invalid_evaluator"}


def test_low_relevance_is_explicit_with_measured_synthetic_threshold(environment, monkeypatch):
    _, documents, provider, actors, scope = environment
    for report in original_reports():
        documents.ingest(report, actors["admin"])
    record = calibrate(documents, original_calibration_cases(), actors["admin"],
                       evaluator_kind="synthetic-lexical")
    documents.install_calibration(record, actors["admin"])
    monkeypatch.setattr(provider, "rerank", lambda query, texts: [record.threshold - 1] * len(texts))
    result = documents.search("a deliberately unsupported query", scope, actors["admin"])
    assert result.hits == [] and result.low_prior is True


def test_deleted_vectors_and_inconsistent_batches_fail_closed(environment, monkeypatch):
    store, documents, provider, actors, scope = environment
    report = original_reports()[0]
    original_embed = provider.embed
    monkeypatch.setattr(provider, "embed", lambda texts: [
        [1.0, 0.0] if i == 0 else [1.0, 0.0, 0.0] for i, _ in enumerate(texts)
    ])
    with pytest.raises(DomainError, match="same-dimensional"):
        documents.ingest(report, actors["admin"])
    monkeypatch.setattr(provider, "embed", original_embed)
    documents.ingest(report, actors["admin"])
    with store.transaction() as db:
        db.execute("DELETE FROM document_vectors WHERE channel='evidence'")
    with pytest.raises(DomainError) as error:
        documents.search("seal ridges", scope, actors["reader"])
    assert error.value.code == "index_integrity"


def test_active_chunk_limit_and_pdf_page_limit(environment, monkeypatch):
    _, documents, _, actors, _ = environment
    monkeypatch.setattr(module, "MAX_INDEX_CHUNKS", 1)
    with pytest.raises(DomainError, match="Active chunk index"):
        documents.ingest(original_reports()[0], actors["admin"])
    monkeypatch.setattr(module, "MAX_PDF_PAGES", 0)
    with pytest.raises(DomainError, match="page limit"):
        parse_upload("one.pdf", pdf_bytes(text="Original synthetic test."))


def test_calibration_rejects_false_dataset_provenance(environment):
    _, documents, _, actors, _ = environment
    cases = original_calibration_cases()
    cases[0] = cases[0].model_copy(update={"query": "A modified fixture question"})
    with pytest.raises(DomainError, match="fixtures were modified"):
        calibrate(documents, cases, actors["admin"], evaluator_kind="synthetic-lexical",
                  dataset_kind="original-synthetic")
    for report in original_reports():
        documents.ingest(report, actors["admin"])
    first = original_reports()[0]
    documents.ingest(first.model_copy(update={"text": first.text + "Changed source."}), actors["admin"])
    with pytest.raises(DomainError, match="source was modified"):
        calibrate(documents, original_calibration_cases(), actors["admin"],
                  evaluator_kind="synthetic-lexical", dataset_kind="original-synthetic")


def test_original_chinese_queries_retrieve_exact_original_source_not_model_evaluation(environment):
    store, _, _, actors, scope = environment
    provider = SyntheticChineseLexicalProvider()
    documents = DocumentStore(store, provider)
    reports = {report.document_id: report for report in original_reports()}
    for report in reports.values():
        assert "原创合成" in report.title and "原创合成报告" in report.text
        assert all(re.search(r"[㐀-鿿]", value) for value in report.summaries.values())
        documents.ingest(report, actors["admin"])
    chinese = original_calibration_cases(language="zh")
    for case in chinese:
        if not case.relevant_document_ids:
            continue
        result = documents.search(case.query, scope, actors["reader"])
        assert result.low_prior is True  # No threshold is invented for the new language.
        assert result.hits[0].document_id in case.relevant_document_ids
        hit = result.hits[0]
        assert re.search(r"[㐀-鿿]", hit.text)
        assert reports[hit.document_id].text[hit.quote_start:hit.quote_end] == hit.text
        assert documents.validate_hit(hit, actors["reader"])
    # ACL exclusion still applies when the incoming question is entirely Chinese.
    documents.set_acl("SYN-PROBE-08", ["admin"], actors["admin"])
    probe = next(case for case in chinese if case.case_id == "eval-probe-zh")
    result = documents.search(probe.query, scope, actors["reader"])
    assert all(hit.document_id != "SYN-PROBE-08" for hit in result.hits)


def test_chinese_fit_eval_labels_remain_disjoint_and_calibration_is_explicit(environment):
    store, _, _, actors, _ = environment
    english = original_calibration_cases()
    chinese = original_calibration_cases(language="zh")
    assert len(english) == len(chinese) == 12
    assert original_heldout_cases(language="zh") == [c for c in chinese if c.split == "eval"]
    assert {c.case_id for c in english}.isdisjoint(c.case_id for c in chinese)
    for en, zh in zip(english, chinese, strict=True):
        assert zh.case_id == en.case_id + "-zh"
        assert zh.split == en.split and zh.relevant_document_ids == en.relevant_document_ids
    fit_docs = {doc for c in english + chinese if c.split == "fit" for doc in c.relevant_document_ids}
    eval_docs = {doc for c in english + chinese if c.split == "eval" for doc in c.relevant_document_ids}
    assert fit_docs.isdisjoint(eval_docs)
    documents = DocumentStore(store, SyntheticChineseLexicalProvider())
    for report in original_reports():
        documents.ingest(report, actors["admin"])
    measured = calibrate(documents, chinese, actors["admin"],
                         evaluator_kind="synthetic-lexical", dataset_kind="original-synthetic")
    assert measured.production_validated is False
    assert measured.provider_fingerprint == "synthetic-lexical-chinese-bigrams-unit-test-v1"
    assert measured.fit_case_ids == [c.case_id for c in chinese if c.split == "fit"]
    assert measured.eval_case_ids == [c.case_id for c in chinese if c.split == "eval"]
    assert measured.selection_objective == "fit_micro_f1_then_precision_then_rejection_then_smaller_k"
    assert measured.candidate_top_ks == [1, 2, 4, 8]  # No metric/grid retuning for Chinese.
    result = documents.search(chinese[0].query, chinese[0].scope, actors["admin"])
    assert result.low_prior is True and result.calibration_version.startswith("uncalibrated-local-")


def test_query_results_and_issued_citations_survive_store_reopen(environment):
    store, documents, provider, actors, scope = environment
    documents.ingest(original_reports()[0], actors["admin"])
    hit = documents.search("seal ridges", scope, actors["reader"]).hits[0]
    reopened = DocumentStore(Store(store.root), provider)
    assert reopened.validate_hit(RetrievalHit.model_validate(hit.model_dump()), actors["reader"])
    assert reopened.drill(hit.document_id, hit.chunk_id, actors["reader"])["text"] == hit.text
