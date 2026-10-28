"""Citation retention and document evidence contracts with synthetic temporary data."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from quality_analysis import documents as module
from quality_analysis.business import example_scope, seed_business
from quality_analysis.documents import SUMMARY_CHANNELS, DocumentStore
from quality_analysis.provider import ModelReply, SyntheticProvider
from quality_analysis.schemas import (
    AnalyzeRequest,
    Confirmation,
    DocumentInput,
    DomainError,
    Scope,
    ToolRequest,
    digest,
)
from quality_analysis.service import AnalysisService
from quality_analysis.storage import Store
from quality_analysis.verifier import Verifier


class SyntheticRetentionProvider:
    fingerprint = "synthetic-lexical-citation-retention-regression-v1"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def rerank(self, query, texts):
        if query == "refresh":
            return [2.0 if text.startswith("Alpha") else 10000.0 for text in texts]
        base = 2 * int(query.removeprefix("query "))
        return [float(base + (2 if text.startswith("Alpha") else 1)) for text in texts]


@pytest.fixture
def retention_environment(tmp_path):
    store = Store(tmp_path)
    actors = {
        name: store.authenticate(store.provision_user(name, role, ["P1"]))
        for name, role in (("target", "admin"), ("filler", "analyst"), ("outsider", "analyst"))
    }
    documents = DocumentStore(store, SyntheticRetentionProvider())
    for name in ("Alpha", "Beta"):
        documents.ingest(DocumentInput(
            document_id=name, title=name, plant_id="P1",
            text=(name + " original synthetic source quotation. ") * 3,
            summaries={channel: "Human-authored summary for " + name for channel in SUMMARY_CHANNELS},
        ), actors["target"])
    scope = Scope(plant_id="P1", product_id="SYNTHETIC", start_date="2025-01-01",
                  end_date="2025-02-01")
    return store, documents, actors, scope


def issued_rows(store):
    with store.connect() as db:
        return {(row["actor_id"], row["document_id"]): dict(row)
                for row in db.execute("SELECT * FROM document_issued_hits ORDER BY id")}


def test_fresh_response_survives_reissuing_oldest_hit_at_capacity(retention_environment, monkeypatch):
    store, documents, actors, scope = retention_environment
    monkeypatch.setattr(module, "MAX_ISSUED_HITS", 4)
    first = documents.search("query 0", scope, actors["target"])
    filler = documents.search("query 1", scope, actors["filler"])
    before = issued_rows(store)
    assert len(before) == module.MAX_ISSUED_HITS
    oldest = before[("target", "Alpha")]
    assert oldest["id"] == min(row["id"] for row in before.values())

    # Beta is a new issuance; Alpha is an exact reissue of the oldest ledger entry.
    response = documents.search("refresh", scope, actors["target"])
    assert [(hit.document_id, hit.score) for hit in response.hits] == [
        ("Beta", 10000.0), ("Alpha", 2.0),
    ]
    assert response.hits[1] == first.hits[0]
    for hit in response.hits:
        assert documents.validate_hit(hit, actors["target"])
        drilled = documents.drill(hit.document_id, hit.chunk_id, actors["target"])
        assert drilled["citations"] == [hit.model_dump(mode="json")]
    for hit in filler.hits:
        assert documents.validate_hit(hit, actors["filler"])

    after = issued_rows(store)
    assert len(after) == module.MAX_ISSUED_HITS
    refreshed = after[("target", "Alpha")]
    assert refreshed["id"] > max(row["id"] for row in before.values())
    assert {key: value for key, value in refreshed.items() if key != "id"} == {
        key: value for key, value in oldest.items() if key != "id"
    }
    # Retention still evicts an older citation which was not part of this response.
    with pytest.raises(DomainError) as error:
        documents.validate_hit(first.hits[1], actors["target"])
    assert error.value.code == "stale_citation"


def test_reissue_refreshes_only_exact_actor_binding(retention_environment):
    store, documents, actors, scope = retention_environment
    first = documents.search("query 0", scope, actors["target"])
    documents.search("query 0", scope, actors["filler"])
    before = issued_rows(store)
    response = documents.search("query 0", scope, actors["target"])
    assert response.hits == first.hits
    after = issued_rows(store)
    assert len(before) == len(after) == 4
    for key, previous in before.items():
        current = after[key]
        assert {k: v for k, v in current.items() if k != "id"} == {
            k: v for k, v in previous.items() if k != "id"
        }
        if key[0] == "target":
            assert current["id"] > max(row["id"] for row in before.values())
        else:
            assert current == previous
    for hit in response.hits:
        for name in ("target", "filler"):
            assert documents.validate_hit(hit, actors[name])
        with pytest.raises(DomainError) as error:
            documents.validate_hit(hit, actors["outsider"])
        assert error.value.code == "stale_citation"
        with pytest.raises(DomainError) as error:
            documents.drill(hit.document_id, hit.chunk_id, actors["outsider"])
        assert error.value.code == "stale_citation"


@pytest.fixture
def citation_environment(tmp_path):
    store = Store(tmp_path)
    seed_business(store)
    scope = example_scope()
    actor = store.authenticate(store.provision_user("document-admin", "admin", [scope.plant_id, "OTHER"]))
    reader = store.authenticate(store.provision_user("document-reader", "analyst", [scope.plant_id]))
    provider = SyntheticProvider()
    documents = DocumentStore(store, provider)
    source = DocumentInput(document_id="HISTORICAL-1", title="Original synthetic historical report",
        plant_id=scope.plant_id, text="An original synthetic historical observation about seal inspection. " * 3,
        summaries={channel: "Original synthetic seal inspection history" for channel in SUMMARY_CHANNELS})
    documents.ingest(source, actor)
    return store, documents, provider, actor, reader, scope, source


def test_real_document_drill_verifier_retains_exact_issued_hit(citation_environment):
    store, documents, _, actor, _, scope, source = citation_environment
    # Historical retrieval deliberately does not apply product or date restrictions.
    scope = Scope.model_validate({**scope.model_dump(mode="json"), "product_id": "UNRELATED",
                                  "start_date": "2020-01-01", "end_date": "2020-02-01"})
    hit = documents.search("seal inspection", scope, actor).hits[0]
    envelope = documents.drill(hit.document_id, hit.chunk_id, actor)
    request = ToolRequest(tool="document", document_id=hit.document_id, chunk_id=hit.chunk_id)
    result = Verifier(store, documents).execute(request, scope, actor)
    assert result.status == "ok" and result.citations == [hit.model_dump(mode="json")]
    assert documents.validate_hit(result.citations[0], actor)
    assert "plant_id" not in result.citations[0] and "citations" not in result.citations[0]
    assert envelope["plant_id"] == source.plant_id
    assert result.payload["text"] == source.text[hit.quote_start:hit.quote_end]
    mark = result.payload["provenance"]
    assert mark["scope_binding"] == "plant_and_document_acl_only"
    assert mark["request_scope"] == scope.model_dump(mode="json")
    assert mark["request_scope_hash"] == digest(mark["request_scope"])
    assert not {"synthetic", "dataset_id", "data_revision", "scope", "scope_hash"} & mark.keys()
    assert mark["document"]["source_manifest_hash"] == envelope["source_manifest_hash"]
    assert mark["document"]["provider_fingerprint"] == documents.provider.fingerprint
    assert mark["document"]["content_hash"] == hit.content_hash


@pytest.mark.parametrize("change", ["unissued", "version", "acl", "deleted", "wrong_plant", "forged_chunk"])
def test_real_document_verifier_rejects_invalid_authorization(citation_environment, change):
    store, documents, _, actor, reader, scope, source = citation_environment
    hit = documents.search("seal inspection", scope, reader).hits[0]
    if change == "unissued":
        reader = store.authenticate(store.provision_user("unissued-reader", "analyst", [scope.plant_id]))
    elif change == "version":
        documents.ingest(source.model_copy(update={"text": source.text + " New synthetic version."}), actor)
    elif change == "acl":
        documents.set_acl(source.document_id, [actor.actor_id], actor)
    elif change == "deleted":
        documents.delete(source.document_id, actor)
    elif change == "wrong_plant":
        hit = documents.search("seal inspection", scope, actor).hits[0]
        reader = actor
        scope = scope.model_copy(update={"plant_id": "OTHER"})
    else:
        hit = hit.model_copy(update={"chunk_id": "FORGED"})
    with pytest.raises(DomainError) as error:
        Verifier(store, documents).execute(ToolRequest(tool="document", document_id=hit.document_id,
                                                     chunk_id=hit.chunk_id), scope, reader)
    assert error.value.code == {
        "unissued": "stale_citation", "version": "stale_citation", "acl": "document_unavailable",
        "deleted": "document_unavailable", "wrong_plant": "forbidden", "forged_chunk": "stale_citation",
    }[change]


@pytest.mark.parametrize("change", ["missing", "empty", "extra_field", "text", "score", "false_validator"])
def test_issued_adapter_cannot_substitute_enriched_or_forged_citation(citation_environment, change):
    store, documents, _, actor, _, scope, _ = citation_environment
    hit = documents.search("seal inspection", scope, actor).hits[0]
    envelope = deepcopy(documents.drill(hit.document_id, hit.chunk_id, actor))
    if change == "missing":
        envelope.pop("citations")
    elif change == "empty":
        envelope["citations"] = []
    elif change == "extra_field":
        envelope["citations"][0]["plant_id"] = scope.plant_id
    elif change == "text":
        envelope["text"] = "X" * len(envelope["text"])
    elif change == "score":
        envelope["score"] += 1
        envelope["citations"][0]["score"] += 1

    class Adapter:
        def drill(self, *args):
            return envelope

        def validate_hit(self, value, current):
            return False if change == "false_validator" else documents.validate_hit(value, current)

    with pytest.raises(DomainError) as error:
        Verifier(store, Adapter()).execute(ToolRequest(tool="document", document_id=hit.document_id,
                                                     chunk_id=hit.chunk_id), scope, actor)
    assert error.value.code in {"invalid_citation", "stale_citation"}


def test_real_document_evidence_survives_strict_report_validation(citation_environment):
    store, documents, _, actor, _, scope, _ = citation_environment

    class DocumentProvider(SyntheticProvider):
        def complete(self, stage, context, schema):
            reply = super().complete(stage, context, schema)
            value = reply.value.model_dump(mode="json")
            if stage == "plan":
                value["subquestions"][0]["sources"].append("document")
            elif stage == "hypothesizer":
                hit = context["prior"]["hits"][0]
                value["hypotheses"][0]["tools"].append({"tool": "document",
                    "document_id": hit["document_id"], "chunk_id": hit["chunk_id"]})
            return ModelReply(schema.model_validate(value), reply.tokens)

    provider = DocumentProvider()
    service = AnalysisService(store, provider, documents, Verifier(store, documents))
    prepared = service.analyze(AnalyzeRequest(request_id="document-chain", question="seal inspection",
                                              scope=scope), actor)
    assert prepared["status"] == "awaiting_confirmation"
    result = service.confirm(prepared["session_id"], Confirmation(action_id="document-confirm",
        plan_fingerprint=prepared["plan_fingerprint"], selected_sq_ids=["core-rate"]), actor)
    assert result["status"] == "completed", result.get("last_error")
    evidence = result["report"]["sections"][0]["evidence"]
    assert {item["source"] for item in evidence} == {"sql", "document"}
    shown = next(item for item in evidence if item["source"] == "document")
    assert documents.validate_hit(shown["citations"][0], actor)
    assert shown["payload"]["provenance"]["scope_binding"] == "plant_and_document_acl_only"
    with store.connect() as db:
        row = db.execute("SELECT payload_ref FROM entities WHERE entity_id=?", (shown["evidence_id"],)).fetchone()
    raw = store.read_artifact(json.loads(row[0]))["result"]
    assert raw["citations"] == shown["citations"]
