import json

import pytest

from quality_analysis.provider import LocalProvider, SyntheticProvider, local_url
from quality_analysis.schemas import DomainError, Hypotheses, Intent, Plan, Reflection


@pytest.mark.parametrize("url", ["https://example.org", "http://localhost:1", "http://127.0.0.1.evil:3",
    "http://127.0.0.1@evil/", "http://user:pass@127.0.0.1:9", "http://10.0.0.1:9",
    "http://127.0.0.1:1/?token=x", "file:///tmp/model", "https://127.0.0.1:9"])
def test_provider_refuses_non_loopback_or_secret_endpoints(url):
    with pytest.raises(DomainError):
        local_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://[::1]:8080/"])
def test_numeric_loopback_endpoint(url):
    assert local_url(url) == url.rstrip("/")


def test_test_provider_is_explicit_and_structured():
    provider = SyntheticProvider()
    assert provider.fingerprint.startswith("synthetic-test")
    assert provider.complete("intent", {}, Intent).value.kind == "quality_analysis"
    plan = provider.complete("plan", {}, Plan).value
    assert plan.closure(["orders"]) == ["core-rate", "orders"]
    assert provider.complete("hypothesizer", {"sq": {"sq_id": "core-rate"}}, Hypotheses).value.hypotheses
    assert provider.complete("reflection", {}, Reflection).value.action == "no_action"


def test_no_runtime_is_not_synthetic_success():
    provider = LocalProvider("http://127.0.0.1:1", "http://127.0.0.1:1", "http://127.0.0.1:1",
                             "local-generation", "local-embedding", "local-rerank")
    with pytest.raises(DomainError) as error:
        provider.complete("intent", {}, Intent)
    assert error.value.code == "provider_unavailable"


def test_structured_reply_bad_usage_and_truncation(monkeypatch):
    provider = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    base = {"choices": [{"message": {"content": '{"kind":"quality_analysis","explanation":"test"}'},
                         "finish_reason": "stop"}], "usage": {"total_tokens": 12}}
    monkeypatch.setattr(provider, "_post", lambda *args: base)
    assert provider.complete("intent", {}, Intent).tokens == 12
    base["usage"]["total_tokens"] = True
    with pytest.raises(DomainError):
        provider.complete("intent", {}, Intent)
    base["usage"]["total_tokens"] = 12
    base["choices"][0]["finish_reason"] = "length"
    with pytest.raises(DomainError):
        provider.complete("intent", {}, Intent)


@pytest.mark.parametrize("choices", [None, {}, [], [None], ["bad"], [{}],
    [{"message": None}], [{"message": "bad"}], [{"message": {"content": {}}}]])
def test_malformed_choice_envelope_is_known_invalid(monkeypatch, choices):
    provider = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    monkeypatch.setattr(provider, "_post", lambda *args: {
        "choices": choices, "usage": {"total_tokens": 20}})
    with pytest.raises(DomainError) as error:
        provider.complete("intent", {}, Intent)
    assert error.value.code == "provider_invalid"


@pytest.mark.parametrize("group", [None, "line_id", "material_lot", "shift", "day", "supplier_id"])
def test_plan_schema_requires_explicit_grouping(monkeypatch, group):
    provider = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    captured = []
    plan = {"subquestions": [
        {"sq_id": "core", "title": "Compare authorized observations", "tier": "core",
         "sources": ["sql"], "group_by": group},
        {"sq_id": "orders", "title": "Review scoped work orders", "tier": "extended",
         "depends_on": ["core"], "sources": ["work_orders"], "group_by": None},
    ]}

    def post(url, payload):
        captured.append(payload)
        return {"choices": [{"message": {"content": json.dumps(plan)}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 20}}

    monkeypatch.setattr(provider, "_post", post)
    reply = provider.complete("plan", {}, Plan)
    assert [sq.group_by for sq in reply.value.subquestions] == [group, None]
    assert all("group_by" in sq.model_fields_set for sq in reply.value.subquestions)
    assert reply.tokens == 20
    subquestion = captured[0]["response_format"]["json_schema"]["schema"]["$defs"]["Subquestion"]
    assert "group_by" in subquestion["required"]
    assert subquestion["properties"]["group_by"]["enum"] == [
        "line_id", "material_lot", "shift", "day", "supplier_id", None]
    assert "default" not in subquestion["properties"]["group_by"]
    assert Plan.model_json_schema()["$defs"]["Subquestion"]["properties"]["group_by"]["default"] is None
    assert captured[0]["max_tokens"] == 2200
    assert "group_by" not in Plan.model_json_schema()["$defs"]["Subquestion"]["required"]

    # The shared schema stays backward-compatible, but model output must be explicit
    # for every SQ, including a non-SQL extended SQ whose grouping is null.
    for index, sq in enumerate(plan["subquestions"]):
        explicit_group = sq.pop("group_by")
        assert Plan.model_validate(plan).subquestions[index].group_by is None
        before = len(captured)
        with pytest.raises(DomainError) as error:
            provider.complete("plan", {}, Plan)
        assert error.value.code == "provider_invalid"
        assert len(captured) == before + 1
        sq["group_by"] = explicit_group


def test_embedding_rerank_order_and_shape(monkeypatch):
    provider = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    monkeypatch.setattr(provider, "_post", lambda *args: {"data": [
        {"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})
    assert provider.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    monkeypatch.setattr(provider, "_post", lambda *args: {"results": [
        {"index": 1, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.9}]})
    assert provider.rerank("a", ["a", "b"]) == [0.9, 0.1]
    with pytest.raises(DomainError):
        provider.rerank("a", ["a"])


def test_hypothesis_schema_constrains_tools_to_current_sq(monkeypatch):
    provider = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    captured = []
    def post(url, payload):
        captured.append(payload)
        return {"choices": [{"message": {"content": '{"hypotheses":[{"hypothesis_key":"h1",'
            '"statement":"只核查元数据", "tools":[{"tool":"metadata"}]}]}'}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 20}}
    monkeypatch.setattr(provider, "_post", post)
    provider.complete("hypothesizer", {"sq": {"sources": ["metadata"]}}, Hypotheses)
    schema = captured[0]["response_format"]["json_schema"]["schema"]
    variant = schema["$defs"]["ToolRequest"]["anyOf"][0]
    assert variant["properties"] == {"tool": {"type": "string", "enum": ["metadata"]}}
    assert variant["required"] == ["tool"] and variant["additionalProperties"] is False
    assert '"$defs"' not in captured[0]["messages"][0]["content"]
    with pytest.raises(DomainError) as error:
        provider.complete("hypothesizer", {"sq": {"sources": ["sql"], "group_by": "line_id"}}, Hypotheses)
    assert error.value.code == "provider_invalid"
    sql_variant = captured[1]["response_format"]["json_schema"]["schema"]["$defs"]["ToolRequest"]["anyOf"][0]
    assert sql_variant["properties"]["group_by"]["enum"] == ["line_id"]
    assert "group_by" in sql_variant["required"]
    assert Hypotheses.model_json_schema()["$defs"]["ToolRequest"]["properties"]["tool"]["enum"] == [
        "sql", "metadata", "work_orders", "document"]
    with pytest.raises(DomainError) as error:
        provider.complete("hypothesizer", {}, Hypotheses)
    assert error.value.code == "model_scope"


@pytest.mark.parametrize("stage,schema", [("hypothesizer", Hypotheses), ("reflection", Reflection)])
@pytest.mark.parametrize("tool,valid", [
    ({"tool": "sql", "sql": "SELECT line_id,COUNT(*) FROM inspections GROUP BY line_id", "group_by": "line_id"}, True),
    ({"tool": "metadata"}, True),
    ({"tool": "work_orders", "work_order_id": "WO-1"}, True),
    ({"tool": "document", "document_id": "DOC-1", "chunk_id": "chunk-1"}, True),
    ({"tool": "sql", "sql": "SELECT line_id FROM inspections"}, False),
    ({"tool": "sql", "sql": "SELECT line_id FROM inspections", "group_by": "shift"}, False),
    ({"tool": "sql", "sql": "SELECT line_id FROM inspections", "group_by": "line_id", "document_id": "sq-001"}, False),
    ({"tool": "sql", "sql": "SELECT line_id FROM inspections", "group_by": "line_id", "document_id": None}, False),
    ({"tool": "sql", "sql": "SELECT line_id FROM inspections", "group_by": "line_id", "parameters": {}}, False),
    ({"tool": "sql", "sql": "SELECT line_id FROM inspections", "group_by": "line_id", "parameters": {"min_group_n": 20}}, False),
    ({"tool": "sql", "sql": "SELECT line_id FROM inspections", "group_by": "line_id", "parameters": None}, False),
    ({"tool": "metadata", "group_by": "line_id"}, False),
    ({"tool": "metadata", "sql": "SELECT 1"}, False),
    ({"tool": "work_orders", "group_by": "line_id"}, False),
    ({"tool": "document", "document_id": "DOC-1", "chunk_id": "chunk-1", "group_by": "line_id"}, False),
])
def test_tool_specific_schema_and_returned_contract(monkeypatch, stage, schema, tool, valid):
    provider = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    hypothesis = {"hypothesis_key": "h1", "statement": "Bounded observation", "tools": [tool]}
    value = ({"hypotheses": [hypothesis]} if stage == "hypothesizer" else
             {"action": "repair", "reason": "Repair current SQ", "replacement": hypothesis})
    captured = []
    def post(url, payload):
        captured.append(payload)
        return {"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 20}}
    monkeypatch.setattr(provider, "_post", post)
    context = {"sq": {"sources": ["sql", "metadata", "work_orders", "document"], "group_by": "line_id"}}
    if valid:
        assert provider.complete(stage, context, schema).tokens == 20
    else:
        with pytest.raises(DomainError) as error:
            provider.complete(stage, context, schema)
        assert error.value.code == "provider_invalid"
    assert len(captured) == 1  # No retry or silent field removal.
    variants = captured[0]["response_format"]["json_schema"]["schema"]["$defs"]["ToolRequest"]["anyOf"]
    by_tool = {branch["properties"]["tool"]["enum"][0]: branch for branch in variants}
    for name, fields in {"sql": {"tool", "sql", "group_by"}, "metadata": {"tool"},
                         "work_orders": {"tool", "work_order_id"}, "document": {"tool", "document_id", "chunk_id"}}.items():
        assert set(by_tool[name]["properties"]) == fields
        assert by_tool[name]["additionalProperties"] is False
    assert by_tool["sql"]["properties"]["group_by"] == {"enum": ["line_id"]}
    assert "parameters" not in by_tool["sql"]["properties"]
    assert "只用字面量，不使用占位符，不提供parameters" in captured[0]["messages"][0]["content"]
    assert "parameters" in Hypotheses.model_json_schema()["$defs"]["ToolRequest"]["properties"]
    assert set(by_tool["sql"]["required"]) == {"tool", "sql", "group_by"}
    assert set(by_tool["document"]["required"]) == {"tool", "document_id", "chunk_id"}


def test_overall_sql_requires_explicit_null_group(monkeypatch):
    provider = LocalProvider(*(["http://127.0.0.1:1"] * 3), "g", "e", "r")
    value = {"hypotheses": [{"hypothesis_key": "h1", "statement": "Overall observation", "tools": [
        {"tool": "sql", "sql": "SELECT COUNT(*) FROM inspections", "group_by": None}]}]}
    monkeypatch.setattr(provider, "_post", lambda *args: {
        "choices": [{"message": {"content": json.dumps(value)}}], "usage": {"total_tokens": 20}})
    result = provider.complete("hypothesizer", {"sq": {"sources": ["sql"], "group_by": None}}, Hypotheses)
    assert result.value.hypotheses[0].tools[0].group_by is None
