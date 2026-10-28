"""Explicit SQ grouping is authorization, not a model suggestion."""
import json
import sqlite3

import pytest
from pydantic import ValidationError
from test_graph import app as graph_app
from test_graph import confirmation, request

from quality_analysis.provider import ModelReply, SyntheticProvider
from quality_analysis.schemas import (
    DomainError,
    Hypotheses,
    Plan,
    Reflection,
    Subquestion,
    ToolRequest,
    ToolResult,
    digest,
)
from quality_analysis.service import AnalysisService


@pytest.fixture
def app(tmp_path):
    return graph_app.__wrapped__(tmp_path)


def sql_tool(group, sql=None):
    columns = f"{group}," if group else ""
    grouping = f" GROUP BY {group}" if group else ""
    return {"tool": "sql", "group_by": group,
            "sql": sql or f"SELECT {columns}COUNT(*) AS n,SUM(defect) AS defects FROM inspections{grouping}"}


class ScopeProvider(SyntheticProvider):
    def __init__(self, tools, *, group="line_id", reflection=None):
        self.tools, self.group, self.reflection = tools, group, reflection
        self.contexts = []

    def complete(self, stage, context, schema):
        self.contexts.append((stage, context))
        if stage == "plan":
            return ModelReply(Plan.model_validate({"subquestions": [
                {"sq_id": "core-rate", "title": "Current line comparison" if self.group else "Whole cohort defect rate",
                 "tier": "core", "sources": ["sql"], "group_by": self.group},
                {"sq_id": "lot-strata", "title": "Future material comparison", "tier": "extended",
                 "sources": ["sql"], "group_by": "material_lot", "depends_on": ["core-rate"]},
            ]}), 20)
        if stage == "hypothesizer":
            return ModelReply(Hypotheses.model_validate({"hypotheses": [
                {"hypothesis_key": f"hyp-{i}", "statement": "Explicit scope boundary test", "tools": [tool]}
                for i, tool in enumerate(self.tools)
            ]}), 20)
        if stage == "reflection":
            value = ({"action": "repair", "reason": "Try another grouping", "replacement": {
                "hypothesis_key": "reflected", "statement": "Reflected scope boundary test", "tools": [self.reflection]}}
                if self.reflection else {"action": "no_action", "reason": "Do not expand scope"})
            return ModelReply(Reflection.model_validate(value), 20)
        return super().complete(stage, context, schema)


def test_core_cannot_dispatch_material_or_shift_hypotheses(app):
    service, actor, _, verifier = app
    service.provider = ScopeProvider([sql_tool("line_id"), sql_tool("material_lot"), sql_tool("shift")])
    req = request().model_copy(update={"question": "Compare lines now; examine materials and shifts later"})
    prepared = service.analyze(req, actor)
    result = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert result["status"] == "limited"
    assert [r.group_by for r in verifier.requests if r.tool == "sql"] == ["line_id"]
    assert result["tools"] == 2  # cohort plus authorized line query only
    assert result["authorized"] == ["core-rate"]
    assert len(result["report"]["sections"][0]["evidence"]) == 1
    with service.store.connect() as db:
        guards = db.execute("SELECT payload_ref FROM entities WHERE kind='GuardAssessment'").fetchall()
    rejected = [service.store.read_artifact(json.loads(row[0])) for row in guards]
    assert sum(item.get("code") == "sq_grouping" for item in rejected) == 2
    contexts = dict(service.provider.contexts)
    assert "group_by" in contexts["plan"]["grouping_contract"]
    for stage in ("hypothesizer", "reflection"):
        context = contexts[stage]
        assert context["request"]["question"] == "Current line comparison"
        assert context["request"]["question"] != req.question
        assert "GROUP BY line_id" in context["sql_contract"]
        assert "GROUP BY material_lot" not in context["sql_contract"]


@pytest.mark.parametrize("group", ["material_lot", "shift"])
def test_reflection_cannot_dispatch_different_group(app, group):
    service, actor, _, verifier = app
    verifier.reject_sql = True
    service.provider = ScopeProvider([sql_tool("line_id")], reflection=sql_tool(group))
    prepared = service.analyze(request(), actor)
    result = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert result["status"] == "limited"
    assert [r.group_by for r in verifier.requests if r.tool == "sql"] == ["line_id"]
    assert result["tools"] == 2
    assert result["last_error"]["code"] == "sq_grouping"
    assert result["report"]["sections"][0]["evidence"] == []
    assert result["trace"].count("reflection") == 1


@pytest.mark.parametrize("group,sql", [
    ("line_id", "SELECT material_lot,COUNT(*) FROM inspections GROUP BY material_lot"),
    ("line_id", "SELECT line_id,shift,COUNT(*) FROM inspections GROUP BY line_id,shift"),
    ("line_id", "SELECT COUNT(*) FROM inspections"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY 1"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY LOWER(line_id)"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY ROLLUP(line_id)"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY GROUPING SETS ((line_id),())"),
    ("line_id", "SELECT material_lot AS line_id,COUNT(*) FROM inspections GROUP BY line_id"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY line_id; SELECT shift FROM inspections"),
    ("line_id", "SELECT line_id,COUNT(*) FROM (SELECT shift AS line_id FROM inspections) GROUP BY line_id"),
    ("line_id", "SELECT 'unterminated FROM inspections GROUP BY line_id"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY"),
    (None, "SELECT line_id,COUNT(*) FROM inspections GROUP BY line_id"),
    (None, "SELECT order_id,issue,resolution FROM work_orders ORDER BY order_id"),
    (None, "SELECT COUNT(*) FROM work_orders"),
    (None, "SELECT * FROM inspections"),
    (None, "SELECT defect FROM inspections"),
    (None, "SELECT line_id,COUNT(*) FROM inspections"),
    ("line_id", "SELECT line_id,SUM(CASE WHEN shift='DAY' THEN defect ELSE 0 END) FROM inspections GROUP BY line_id"),
    ("line_id", "SELECT line_id,SUM(defect*(shift='NIGHT')) FROM inspections GROUP BY line_id"),
    ("line_id", "SELECT line_id,MAX(material_lot),COUNT(*) FROM inspections GROUP BY line_id"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY line_id HAVING MAX(shift)='DAY'"),
    ("line_id", "SELECT line_id,COUNT(*) FROM inspections GROUP BY line_id ORDER BY MAX(material_lot)"),
    ("line_id", "SELECT line_id,SUM(rework) FROM inspections GROUP BY line_id"),
    ("line_id", "SELECT line_id,defect,COUNT(*) FROM inspections GROUP BY line_id"),
])
def test_sql_ast_mismatch_is_rejected_before_any_sql_dispatch(app, group, sql):
    service, actor, _, verifier = app
    service.provider = ScopeProvider([sql_tool(group, sql)], group=group)
    prepared = service.analyze(request(), actor)
    result = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert result["status"] == "limited"
    assert not any(r.tool == "sql" for r in verifier.requests)
    assert result["tools"] == 1
    assert result["last_error"]["code"] == "sq_grouping"
    assert result["report"]["sections"][0]["evidence"] == []
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE kind='evidence'").fetchone()[0] == 0


@pytest.mark.parametrize("sql", [
    "SELECT line_id,COUNT(*) FROM inspections GROUP BY line_id",
    'SELECT "line_id",COUNT(*) FROM inspections GROUP BY "line_id"',
    "SELECT i.line_id,COUNT(*) FROM inspections AS i GROUP BY i.line_id",
    "SELECT LINE_ID,COUNT(*) FROM inspections GROUP BY LINE_ID",
])
def test_exact_group_ast_is_accepted(sql):
    AnalysisService._guard_sq_grouping({"group_by": "line_id"}, ToolRequest.model_validate(sql_tool("line_id", sql)))


@pytest.mark.parametrize("group,metric,expression", [
    (None, "defect_rate", "SUM(defect)"),
    ("material_lot", "rework_rate", "SUM(rework)"),
    ("shift", "cycle_time_mean", "AVG(cycle_time)"),
    ("line_id", "defect_rate", "SUM(CASE WHEN defect=1 THEN 1 ELSE 0 END)"),
])
def test_metric_aggregate_contract_preserves_legal_queries(group, metric, expression):
    prefix = f"{group}," if group else ""
    suffix = f" GROUP BY {group}" if group else ""
    sql = f"SELECT {prefix}COUNT(*) AS n,{expression} AS value FROM inspections{suffix} HAVING n>=20 ORDER BY value"
    AnalysisService._guard_sq_grouping({"group_by": group},
        ToolRequest.model_validate(sql_tool(group, sql)), metric)


def test_reflection_cannot_smuggle_conditional_group(app):
    service, actor, _, verifier = app
    verifier.reject_sql = True
    sql = "SELECT line_id,SUM(CASE WHEN shift='DAY' THEN defect ELSE 0 END) FROM inspections GROUP BY line_id"
    service.provider = ScopeProvider([sql_tool("line_id")], reflection=sql_tool("line_id", sql))
    prepared = service.analyze(request(), actor)
    result = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert result["status"] == "limited" and result["last_error"]["code"] == "sq_grouping"
    assert len([r for r in verifier.requests if r.tool == "sql"]) == 1
    assert result["report"]["sections"][0]["evidence"] == []


def test_null_group_returns_truthful_whole_cohort_aggregate(app):
    service, actor, _, boundary = app
    service.provider = ScopeProvider([sql_tool(None)], group=None)

    class CohortVerifier:
        def __init__(self):
            self.sql_requests = []

        def execute(self, tool, scope, actor):
            if tool.tool != "sql":
                return boundary.execute(tool, scope, actor)
            self.sql_requests.append(tool)
            with sqlite3.connect(":memory:") as db:
                db.row_factory = sqlite3.Row
                db.execute("CREATE TABLE inspections(line_id TEXT,defect INTEGER)")
                db.executemany("INSERT INTO inspections VALUES(?,?)", [("a", 1), ("a", 0), ("b", 1), ("b", 1)])
                rows = [dict(row) for row in db.execute(tool.sql)]
            return ToolResult(status="ok", summary="Whole authorized synthetic cohort", source="sqlite-test",
                              payload={"group_by": tool.group_by, "rows": rows})

    verifier = CohortVerifier()
    service.verifier = verifier
    prepared = service.analyze(request(), actor)
    assert prepared["plan"]["subquestions"][0]["group_by"] is None
    result = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert result["status"] == "completed"
    assert len(verifier.sql_requests) == 1 and verifier.sql_requests[0].group_by is None
    evidence = result["report"]["sections"][0]["evidence"][0]
    assert evidence["payload"] == {"group_by": None, "rows": [{"n": 4, "defects": 3}]}
    contract = dict(service.provider.contexts)["hypothesizer"]["sql_contract"]
    assert "不得含GROUP BY" in contract
    assert "GROUP BY line_id" not in contract


def test_metadata_cannot_complete_sql_metric_sq(app):
    service, actor, _, verifier = app

    class MetadataOnly(ScopeProvider):
        def complete(self, stage, context, schema):
            reply = super().complete(stage, context, schema)
            if stage == "plan":
                reply.value.subquestions[0].sources = ["sql", "metadata"]
            return reply

    service.provider = MetadataOnly([{"tool": "metadata"}])
    prepared = service.analyze(request(), actor)
    result = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert result["status"] == "limited" and result["last_error"]["code"] == "metric_evidence_missing"
    assert result["report"]["sections"][0]["outcome"] == "unresolved"
    assert not any(r.tool == "sql" for r in verifier.requests)


def test_grouping_default_is_whole_cohort_and_fingerprint_covers_grouping():
    sq = {"sq_id": "core", "title": "Whole cohort", "tier": "core", "sources": ["sql"]}
    overall = Subquestion.model_validate(sq)
    assert overall.group_by is None
    plan = Plan(subquestions=[overall]).model_dump()
    grouped = Plan(subquestions=[Subquestion.model_validate({**sq, "group_by": "line_id"})]).model_dump()
    assert digest({"plan": plan, "manifest": {}}) != digest({"plan": grouped, "manifest": {}})
    with pytest.raises(ValidationError):
        Subquestion.model_validate({**sq, "group_by": "plant_id"})
    with pytest.raises(DomainError) as error:
        AnalysisService._guard_sq_grouping(sq, ToolRequest.model_validate(sql_tool("line_id")))
    assert error.value.code == "sq_grouping"
