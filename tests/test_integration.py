import json

import pytest

from quality_analysis.business import example_scope, seed_business
from quality_analysis.documents import DocumentStore, original_reports
from quality_analysis.provider import LocalProvider, ModelReply, SyntheticProvider
from quality_analysis.retrieval_calibration import (
    calibrate,
    original_calibration_cases,
)
from quality_analysis.schemas import AnalyzeRequest, Confirmation, FollowupRequest, Hypotheses
from quality_analysis.service import AnalysisService
from quality_analysis.storage import Store
from quality_analysis.verifier import Verifier


@pytest.fixture
def integrated(tmp_path):
    store = Store(tmp_path)
    seed_business(store)
    scope = example_scope()
    token = store.provision_user("integration-admin", "admin", [scope.plant_id])
    actor = store.authenticate(token)
    provider = SyntheticProvider()
    documents = DocumentStore(store, provider)
    for doc in original_reports(scope.plant_id):
        documents.ingest(doc, actor)
    result = calibrate(documents, original_calibration_cases(scope.plant_id), actor,
                       evaluator_kind="synthetic-lexical", dataset_kind="original-synthetic")
    documents.install_calibration(result, actor)
    return AnalysisService(store, provider, documents, Verifier(store, documents)), actor, scope


def test_actual_business_documents_graph_and_authorized_followup(integrated):
    service, actor, scope = integrated
    prepared = service.analyze(AnalyzeRequest(request_id="original-e2e", question="比较产线缺陷率并检查材料批次差异",
                                              scope=scope), actor)
    assert prepared["status"] == "awaiting_confirmation", prepared.get("last_error")
    result = service.confirm(prepared["session_id"], Confirmation(action_id="confirm",
        plan_fingerprint=prepared["plan_fingerprint"], selected_sq_ids=["core-rate"]), actor)
    assert result["status"] == "completed", result.get("last_error")
    assert result["provider"].startswith("synthetic-test")
    evidence = result["report"]["sections"][0]["evidence"]
    assert evidence and evidence[0]["payload"]
    assert "synthetic-test" not in evidence[0]["source"]  # actual business engine, not graph test double
    followup = service.followup(result["session_id"], FollowupRequest(request_id="follow",
        question="按材料批次分层检查", selected_sq_ids=["lot-strata"]), actor)
    assert followup["status"] == "completed", followup.get("last_error")
    assert len(followup["report"]["sections"]) == 2
    assert followup["round_id"] != result["round_id"]


def test_raw_sql_projection_remains_audit_only(integrated):
    service, actor, scope = integrated

    class FalseProjection(SyntheticProvider):
        def complete(self, stage, context, schema):
            if stage == "hypothesizer":
                return ModelReply(Hypotheses.model_validate({"hypotheses": [{
                    "hypothesis_key": "untrusted", "statement": "Unverified model hypothesis",
                    "tools": [{"tool": "sql", "group_by": "line_id", "sql":
                        "SELECT line_id,COUNT(*) AS n,0 AS defects FROM inspections GROUP BY line_id"}]}]}), 20)
            return super().complete(stage, context, schema)

    service.provider = FalseProjection()
    prepared = service.analyze(AnalyzeRequest(request_id="raw-projection", question="Compare lines", scope=scope), actor)
    result = service.confirm(prepared["session_id"], Confirmation(action_id="confirm-projection",
        plan_fingerprint=prepared["plan_fingerprint"], selected_sq_ids=["core-rate"]), actor)
    assert result["status"] == "completed"
    shown = result["report"]["sections"][0]["evidence"][0]
    assert "rows" not in shown["payload"] and "executed_sql" not in shown["payload"]
    assert [g["numerator"] for g in shown["payload"]["statistics"]["groups"]] == [155, 780]
    with service.store.connect() as db:
        row = db.execute("SELECT payload_ref FROM entities WHERE entity_id=?", (shown["evidence_id"],)).fetchone()
    raw = service.store.read_artifact(json.loads(row[0]))["result"]["payload"]
    assert [r["defects"] for r in raw["rows"]] == [0, 0]


def test_malformed_model_reply_is_failed_not_left_started(integrated, monkeypatch):
    service, actor, scope = integrated
    prepared = service.analyze(AnalyzeRequest(request_id="bad-envelope", question="Compare lines", scope=scope), actor)
    local = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    monkeypatch.setattr(local, "_post", lambda *args: {"choices": [None], "usage": {"total_tokens": 20}})
    monkeypatch.setattr(service.provider, "complete", local.complete)
    result = service.confirm(prepared["session_id"], Confirmation(action_id="confirm-envelope",
        plan_fingerprint=prepared["plan_fingerprint"], selected_sq_ids=["core-rate"]), actor)
    assert result["status"] == "blocked" and result["last_error"]["code"] == "provider_invalid"
    calls = service.calls(prepared["session_id"], actor)
    hypothesis = [c for c in calls if c["kind"] == "hypothesizer"]
    assert len(hypothesis) == 1 and hypothesis[0]["status"] == "failed"
    assert hypothesis[0]["error_code"] == "provider_invalid"
    assert not any(c["status"] == "started" for c in calls)


@pytest.mark.parametrize("metric", ["defect_rate", "rework_rate", "cycle_time_mean"])
def test_scope_metric_is_carried_through_real_verifier(integrated, metric):
    service, actor, scope = integrated
    scope = scope.model_copy(update={"metric_id": metric})
    prepared = service.analyze(AnalyzeRequest(request_id="metric-test", question="比较授权产线当前指标",
                                              scope=scope), actor)
    assert prepared["status"] == "awaiting_confirmation", prepared.get("last_error")
    result = service.confirm(prepared["session_id"], Confirmation(action_id="metric-confirm",
        plan_fingerprint=prepared["plan_fingerprint"], selected_sq_ids=["core-rate"]), actor)
    assert result["status"] == "completed", result.get("last_error")
    assert result["report"]["scope"]["metric_id"] == metric
