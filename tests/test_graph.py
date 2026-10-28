"""Graph boundary tests use explicit deterministic adapters, not LLM acceptance."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from quality_analysis.provider import ModelReply, SyntheticProvider
from quality_analysis.schemas import (
    AnalyzeRequest,
    Confirmation,
    DomainError,
    FollowupRequest,
    Hypotheses,
    Reflection,
    RetrievalResult,
    Scope,
    ToolResult,
)
from quality_analysis.service import MAX_TOOLS, NODES, AnalysisService
from quality_analysis.storage import Store, canonical


class EmptyDocuments:
    def __init__(self, store):
        self.store, self.searches = store, 0

    def search(self, query, scope, actor):
        self.searches += 1
        return RetrievalResult(hits=[], low_prior=True, index_revision=self.store.revisions()[1],
                               calibration_version="explicit-test")

    def validate_hit(self, hit, actor):
        raise AssertionError("no test citations should exist")


class BoundaryVerifier:
    """An explicit test double verifies flow, never business/statistical correctness."""
    def __init__(self):
        self.requests = []
        self.reject_sql = False

    def execute(self, request, scope, actor):
        self.requests.append(request)
        if request.tool == "sql" and self.reject_sql:
            raise DomainError("sql_denied", "合成测试拒绝查询")
        return ToolResult(status="ok", summary="明确标记的流程测试工具结果", source="synthetic-test",
                          payload={"scope": scope.model_dump(mode="json"), "count": 40})


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path)
    token = store.provision_user("analyst", "analyst", ["plant-a"])
    actor = store.authenticate(token)
    docs, verifier = EmptyDocuments(store), BoundaryVerifier()
    service = AnalysisService(store, SyntheticProvider(), docs, verifier)
    return service, actor, docs, verifier


def request(key="req", **kwargs):
    scope = Scope(plant_id="plant-a", product_id="product-a", start_date="2025-01-01", end_date="2025-02-01")
    return AnalyzeRequest(request_id=key, question="比较授权产线质量差异", scope=scope, **kwargs)


def confirmation(prepared, key="confirm", selected=None):
    return Confirmation(action_id=key, plan_fingerprint=prepared["plan_fingerprint"],
                        selected_sq_ids=selected or ["core-rate"])


def test_graph_has_exact_thirteen_nodes(app):
    service, *_ = app
    assert set(service.graph.get_graph().nodes) - {"__start__", "__end__"} == set(NODES)


def test_prepare_confirm_followup_new_evidence_ids(app):
    service, actor, docs, verifier = app
    prepared = service.analyze(request(), actor)
    assert prepared["status"] == "awaiting_confirmation"
    assert prepared["trace"] == NODES[:7]
    grouping = {sq["sq_id"]: sq["group_by"] for sq in prepared["plan"]["subquestions"]}
    assert grouping["core-rate"] == "line_id"
    assert grouping["lot-strata"] == "material_lot"
    assert not any(r.tool == "sql" for r in verifier.requests)
    result = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert result["status"] == "completed", result.get("last_error")
    assert result["authorized"] == ["core-rate"]
    assert result["report"]["sections"][0]["evidence"]
    old_ids = {e["evidence_id"] for s in result["report"]["sections"] for e in s["evidence"]}
    new = service.followup(result["session_id"], FollowupRequest(request_id="follow",
        question="检查材料批次差异", selected_sq_ids=["lot-strata"]), actor)
    assert new["status"] == "completed", new.get("last_error")
    assert new["round_id"] != result["round_id"]
    assert new["authorized"] == ["core-rate", "lot-strata"]
    ids = {e["evidence_id"] for s in new["report"]["sections"] for e in s["evidence"]}
    assert not ids & old_ids
    assert new["calls"] > result["calls"]
    assert docs.searches == 1


def test_confirmation_is_immutable_idempotent_and_core_only(app):
    service, actor, _, verifier = app
    prepared = service.analyze(request(), actor)
    with pytest.raises(DomainError) as error:
        service.confirm(prepared["session_id"], confirmation(prepared, selected=["lot-strata"]), actor)
    assert error.value.code == "first_round_core"
    action = confirmation(prepared)
    result = service.confirm(prepared["session_id"], action, actor)
    n = len(verifier.requests)
    assert service.confirm(prepared["session_id"], action, actor) == result
    assert len(verifier.requests) == n
    with pytest.raises(DomainError) as error:
        service.confirm(prepared["session_id"], confirmation(prepared, selected=["orders"]), actor)
    assert error.value.code == "action_conflict"


def test_request_id_replay_and_conflict(app):
    service, actor, docs, _ = app
    value = service.analyze(request(), actor)
    assert service.analyze(request(), actor) == value
    assert docs.searches == 1
    with pytest.raises(DomainError) as error:
        service.analyze(request().model_copy(update={"question": "另一个问题"}), actor)
    assert error.value.code == "request_conflict"


def test_missing_scope_does_not_call_model(app):
    service, actor, docs, verifier = app
    value = service.analyze(AnalyzeRequest(request_id="no-scope", question="质量怎么样"), actor)
    assert value["status"] == "needs_clarification"
    assert value["calls"] == 0 and not verifier.requests and not docs.searches


def test_owner_and_current_plant_isolation(app):
    service, actor, *_ = app
    token = service.store.provision_user("other", "admin", ["plant-b"])
    other = service.store.authenticate(token)
    with pytest.raises(DomainError) as error:
        service.analyze(request(), other)
    assert error.value.status == 403
    value = service.analyze(request(), actor)
    for method in (service.get, service.audit):
        with pytest.raises(DomainError) as error:
            method(value["session_id"], other)
        assert error.value.status == 404


def test_prior_cache_exact_not_semantic_and_invalidated(app):
    service, actor, docs, *_ = app
    service.analyze(request("r1"), actor)
    service.analyze(request("r2"), actor)
    assert docs.searches == 1
    service.analyze(request("r3").model_copy(update={"question": "换一个问法"}), actor)
    assert docs.searches == 2
    with service.store.transaction() as db:
        db.execute("UPDATE settings SET value='1' WHERE key='index_revision'")
    service.analyze(request("r4"), actor)
    assert docs.searches == 3


def test_changed_snapshot_prevents_confirm_and_hides_old_report(app):
    service, actor, *_ = app
    value = service.analyze(request(), actor)
    result = service.confirm(value["session_id"], confirmation(value), actor)
    assert result["report"]["sections"]
    with service.store.transaction() as db:
        db.execute("UPDATE settings SET value='1' WHERE key='index_revision'")
    stale = service.get(value["session_id"], actor)["report"]
    assert stale["stale"] and not stale["sections"]
    with pytest.raises(DomainError) as error:
        service.followup(value["session_id"], FollowupRequest(request_id="f", question="追加批次",
                         selected_sq_ids=["lot-strata"]), actor)
    assert error.value.code == "snapshot_changed"


def test_preflight_rejection_is_guard_not_evidence(app):
    service, actor, _, verifier = app
    verifier.reject_sql = True
    value = service.analyze(request(), actor)
    result = service.confirm(value["session_id"], confirmation(value), actor)
    assert result["status"] == "limited"
    assert result["trace"].count("reflection") == 1
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entities WHERE kind='Evidence'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM entities WHERE kind='GuardAssessment'").fetchone()[0] >= 2
    assert result["report"]["sections"][0]["outcome"] == "unresolved"


class RepairProvider(SyntheticProvider):
    def complete(self, stage, context, schema):
        if stage == "hypothesizer":
            return ModelReply(Hypotheses.model_validate({"hypotheses": [{"hypothesis_key": "wrong",
                "statement": "试图越出授权工具", "tools": [{"tool": "work_orders"}]}]}), 20)
        if stage == "reflection":
            return ModelReply(Reflection.model_validate({"action": "repair", "reason": "改用当前授权SQL指标工具",
                "replacement": {"hypothesis_key": "fixed", "statement": "核验可用指标",
                                "tools": [{"tool": "sql", "group_by": "line_id", "sql":
                                    "SELECT line_id,COUNT(*) AS n,SUM(defect) AS defects FROM inspections GROUP BY line_id"}]}}), 20)
        return super().complete(stage, context, schema)


def test_reflection_creates_new_evidence_and_revision_without_cross_sq(app):
    service, actor, _, verifier = app
    service.provider = RepairProvider()
    value = service.analyze(request(), actor)
    result = service.confirm(value["session_id"], confirmation(value), actor)
    assert result["status"] == "completed", result.get("last_error")
    assert all(r.tool != "work_orders" for r in verifier.requests)
    with service.store.connect() as db:
        revisions = db.execute("SELECT * FROM entities WHERE kind='Revision'").fetchall()
        assert len(revisions) == 2
        final = service.store.read_artifact(json.loads(revisions[-1]["payload_ref"]))
        assert final["supersedes"] == revisions[0]["entity_id"]
        first = service.store.read_artifact(json.loads(revisions[0]["payload_ref"]))
        assert final["claim_id"] == first["claim_id"]
        assert len(final["evidence_ids"]) == 1
    assert result["report"]["sections"][0]["revision_id"] == revisions[-1]["entity_id"]


def test_concurrent_confirmation_executes_once(app):
    service, actor, _, verifier = app
    value = service.analyze(request(), actor)
    def run(i):
        try:
            return service.confirm(value["session_id"], confirmation(value, key="action-" + str(i)), actor)
        except DomainError as error:
            return {"error": error.code}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(r.get("status") == "completed" for r in results) == 1
    assert sum(r.tool == "sql" for r in verifier.requests) == 1


def test_tool_budget_cannot_reset_on_followup(app):
    service, actor, *_ = app
    value = service.analyze(request(), actor)
    service.confirm(value["session_id"], confirmation(value), actor)
    with service.store.transaction() as db:
        state = json.loads(db.execute("SELECT state_json FROM sessions WHERE session_id=?", (value["session_id"],)).fetchone()[0])
        state["tools"] = MAX_TOOLS
        db.execute("UPDATE sessions SET state_json=? WHERE session_id=?", (canonical(state), value["session_id"]))
    result = service.followup(value["session_id"], FollowupRequest(request_id="budget", question="继续分析",
        selected_sq_ids=["orders"]), actor)
    assert result["status"] == "blocked"
    assert result["last_error"]["code"] == "tool_budget"


def test_action_replay_cannot_expose_invalidated_report(app):
    service, actor, *_ = app
    prepared = service.analyze(request(), actor)
    action = confirmation(prepared)
    result = service.confirm(prepared["session_id"], action, actor)
    assert result["report"]["sections"]
    follow = FollowupRequest(request_id="follow", question="检查批次", selected_sq_ids=["lot-strata"])
    service.followup(prepared["session_id"], follow, actor)
    with service.store.transaction() as db:
        db.execute("UPDATE settings SET value='1' WHERE key='index_revision'")
    for call in (lambda: service.confirm(prepared["session_id"], action, actor),
                 lambda: service.followup(prepared["session_id"], follow, actor)):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code == "snapshot_changed"


@pytest.mark.parametrize("metric,expression", [
    ("defect_rate", "SUM(defect)"), ("rework_rate", "SUM(rework)"),
    ("cycle_time_mean", "AVG(cycle_time)"),
])
def test_hypothesizer_receives_current_metric_and_dependency_evidence(app, metric, expression):
    service, actor, *_ = app
    contexts = []

    class RecordingProvider(SyntheticProvider):
        def complete(self, stage, context, schema):
            if stage == "hypothesizer":
                contexts.append(context)
            return super().complete(stage, context, schema)

    service.provider = RecordingProvider()
    req = request()
    req.scope.metric_id = metric
    prepared = service.analyze(req, actor)
    service.confirm(prepared["session_id"], confirmation(prepared), actor)
    result = service.followup(prepared["session_id"], FollowupRequest(request_id="f",
        question="继续检查材料批次", selected_sq_ids=["lot-strata"]), actor)
    assert result["status"] == "completed"
    assert all(expression in ctx["sql_contract"] for ctx in contexts)
    assert all(ctx["request"]["question"] == ctx["sq"]["title"] for ctx in contexts)
    assert "GROUP BY line_id" in contexts[0]["sql_contract"]
    assert "GROUP BY material_lot" in contexts[-1]["sql_contract"]
    assert contexts[0]["dependencies"] == []
    dependency = contexts[-1]["dependencies"][0]
    assert dependency["sq_id"] == "core-rate"
    current_ids = {e["evidence_id"] for s in result["report"]["sections"] for e in s["evidence"]}
    assert all(e["evidence_id"] in current_ids for e in dependency["evidence"])


def test_missing_dependency_decision_blocks_hypothesizer(app):
    service, actor, *_ = app
    prepared = service.analyze(request(), actor)
    _, state = service._owned(prepared["session_id"], actor)
    state.update(queue=["lot-strata"], authorized=["core-rate", "lot-strata"], sq_index=0)
    with pytest.raises(DomainError) as error:
        service._hypothesizer(state)
    assert error.value.code == "dependency_unresolved"
