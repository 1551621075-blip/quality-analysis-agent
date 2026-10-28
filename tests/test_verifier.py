import hashlib
import math
import sqlite3
from contextlib import closing

import pytest

from quality_analysis.business import example_scope, seed_business
from quality_analysis.schemas import DomainError, ToolRequest, digest
from quality_analysis.storage import Store
from quality_analysis.verifier import Verifier


@pytest.fixture
def business(tmp_path):
    store = Store(tmp_path)
    seed_business(store)
    token = store.provision_user("analyst", "analyst", ["PLANT-A"])
    return store, store.authenticate(token), example_scope()


def sql(text, **kwargs):
    return ToolRequest(tool="sql", sql=text, **kwargs)


def test_executes_actual_grouped_sql_and_independent_wilson_math(business):
    store, actor, scope = business
    request = sql(
        "SELECT line_id, COUNT(*) AS n, SUM(defect) AS defects, AVG(defect) AS rate "
        "FROM inspections GROUP BY line_id ORDER BY line_id", group_by="line_id",
    )
    result = Verifier(store).execute(request, scope, actor)
    assert result.status == "ok"
    with closing(sqlite3.connect(store.business_path)) as db:
        expected = db.execute(
            "SELECT line_id,COUNT(*),SUM(defect),AVG(defect) FROM inspections "
            "WHERE plant_id='PLANT-A' AND product_id='WIDGET-A' GROUP BY line_id ORDER BY line_id"
        ).fetchall()
    assert [tuple(row.values()) for row in result.payload["rows"]] == expected
    stats = result.payload["statistics"]
    assert stats["population_n"] == 6200
    for group, (_, n, positives, rate) in zip(stats["groups"], expected, strict=True):
        assert group["n"] == group["denominator"] == n
        assert group["numerator"] == positives
        assert group["estimate"] == rate
        z = 1.959963984540054
        center = (rate + z * z / (2 * n)) / (1 + z * z / n)
        width = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        assert group["ci95"] == pytest.approx([center - width, center + width])
        assert group["significance"] is None
        assert group["adequate"]
    mark = result.payload["provenance"]
    assert mark["scope"] == scope.model_dump(mode="json")
    assert mark["scope_hash"] == digest(mark["scope"])
    assert mark["data_revision"] == store.revisions()[0] == 1
    assert mark["synthetic"] is True


def test_supplier_confounding_and_display_projection_not_trusted(business):
    store, actor, scope = business
    result = Verifier(store).execute(sql(
        "SELECT supplier_id, 999 AS invented_rate FROM inspections "
        "GROUP BY supplier_id HAVING COUNT(*)>0 ORDER BY supplier_id LIMIT 1",
        group_by="supplier_id",
    ), scope, actor)
    assert result.payload["row_count"] == 1
    stats = result.payload["statistics"]
    assert stats["population_n"] == 6200
    assert len(stats["groups"]) == 2
    assert len(stats["stratified_groups"]) == 124
    assert stats["confounding_check"]["marginal_gap"] > 0.1
    assert stats["confounding_check"]["max_within_stratum_gap"] == 0
    assert stats["confounding_check"]["causal_conclusion"] is None
    assert all(g["estimate"] != 999 for g in stats["groups"])


@pytest.mark.parametrize("query", [
    "DELETE FROM inspections", "PRAGMA table_info(inspections)",
    "ATTACH DATABASE '/tmp/external.sqlite' AS x", "SELECT * FROM inspections; SELECT 1",
    "SELECT * FROM inspections -- comment", "SELECT /* comment */ * FROM inspections",
    "SELECT * FROM inspections UNION SELECT * FROM inspections",
    "SELECT * FROM inspections WHERE event_id IN (SELECT event_id FROM inspections)",
    "WITH x AS (SELECT * FROM inspections) SELECT * FROM x",
    "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT * FROM x",
    "SELECT * FROM inspections a JOIN inspections b ON a.event_id=b.event_id",
    "SELECT * FROM inspections,work_orders", "SELECT * FROM main.inspections",
    "SELECT * FROM temp.inspections", "SELECT main.inspections.defect FROM inspections",
    "SELECT * FROM sqlite_master", "SELECT * FROM _dataset", "SELECT * FROM raw_inspections",
    "SELECT rowid FROM inspections", "SELECT _rowid_ FROM inspections",
    "SELECT oid FROM inspections", "SELECT secret FROM inspections",
    "SELECT load_extension('/tmp/payload') FROM inspections",
    "SELECT readfile('/etc/passwd') FROM inspections",
    "SELECT writefile('/tmp/payload','x') FROM inspections",
    "SELECT randomblob(1000000000) FROM inspections",
    "SELECT * FROM pragma_table_info('inspections')",
    "SELECT * FROM inspections WHERE defect=?", "SELECT * FROM inspections LIMIT -1",
    "SELECT * FROM inspections LIMIT 100000", "SELECT * FROM inspections LIMIT :n",
    "SELECT SUM(defect) OVER () FROM inspections", "SELECT CAST(defect AS TEXT) FROM inspections",
    "SELECT event_id AS x FROM inspections WHERE x='outside'",
    "SELECT IIF(defect=1,1,0) FROM inspections LIMIT 1",
    "SELECT COUNT(*) FROM inspections NOT INDEXED",
    "SELECT 1e999 FROM inspections LIMIT 1",
    "SELECT " + "(" * 1000 + "defect" + ")" * 1000 + " FROM inspections LIMIT 1",
])
def test_preflight_rejects_unapproved_sql(business, query):
    store, actor, scope = business
    with pytest.raises(DomainError):
        Verifier(store).execute(sql(query), scope, actor)


def test_named_parameters_bound_not_interpolated_and_exact(business):
    store, actor, scope = business
    verifier = Verifier(store)
    result = verifier.execute(sql(
        "SELECT COUNT(*) AS n FROM inspections WHERE line_id=:line",
        parameters={"line": "LINE-1' OR 1=1 --"},
    ), scope, actor)
    assert result.status == "insufficient"
    assert result.payload["rows"] == [{"n": 0}]
    assert result.payload["statistics"]["population_n"] == 0
    for bindings in ({}, {"wrong": "LINE-1"}, {"line": "LINE-1", "extra": 1}):
        with pytest.raises(DomainError) as error:
            verifier.execute(sql("SELECT COUNT(*) FROM inspections WHERE line_id=:line",
                                 parameters=bindings), scope, actor)
        assert error.value.code == "sql_parameters"
    result = verifier.execute(sql(
        "SELECT COUNT(*) AS n FROM inspections i WHERE i.line_id=:line;",
        parameters={"line": "LINE-1"},
    ), scope, actor)
    assert result.payload["rows"] == [{"n": 3100}]
    assert result.payload["statistics"]["population_n"] == 3100


def test_only_exact_product_date_line_rows_exist_inside_sandbox(business):
    store, actor, original = business
    scope = original.model_copy(update={
        "line_ids": ["LINE-1"], "end_date": original.start_date.replace(day=3),
    })
    verifier = Verifier(store)
    result = verifier.execute(sql(
        "SELECT plant_id,product_id,line_id,MIN(day) AS first,MAX(day) AS last,COUNT(*) AS n "
        "FROM inspections GROUP BY plant_id,product_id,line_id",
    ), scope, actor)
    assert result.payload["rows"] == [{
        "plant_id": "PLANT-A", "product_id": "WIDGET-A", "line_id": "LINE-1",
        "first": "2025-01-01", "last": "2025-01-02", "n": 200,
    }]
    for where in ("plant_id='PLANT-B'", "product_id='WIDGET-B'", "line_id='LINE-2'",
                  "day>='2025-01-03'", "day<'2025-01-01'"):
        result = verifier.execute(sql(f"SELECT COUNT(*) AS n FROM inspections WHERE {where}"), scope, actor)
        assert result.status == "insufficient"
        assert result.payload["rows"] == [{"n": 0}]
    lots = verifier.execute(sql("SELECT * FROM material_lots"), scope, actor)
    assert len(lots.payload["rows"]) == 2
    assert all(row["line_id"] == "LINE-1" and "EARLY" in row["material_lot"]
               for row in lots.payload["rows"])


def test_current_actor_role_plant_and_revocation_enforced(business):
    store, actor, scope = business
    query = sql("SELECT COUNT(*) FROM inspections")
    token = store.provision_user("operator", "operator", ["PLANT-A"])
    operator = store.authenticate(token)
    assert Verifier(store).execute(query, scope, operator).status == "ok"
    with pytest.raises(DomainError):
        Verifier(store).execute(query, scope.model_copy(update={"plant_id": "PLANT-B"}), actor)
    with store.transaction() as db:
        db.execute("UPDATE users SET active=0 WHERE actor_id='analyst'")
    with pytest.raises(DomainError) as error:
        Verifier(store).execute(query, scope, actor)
    assert error.value.code == "unauthorized"


def test_small_cohort_and_cycle_time_statistics(business):
    store, actor, scope = business
    verifier = Verifier(store)
    result = verifier.execute(sql(
        "SELECT COUNT(*) AS n FROM inspections WHERE event_id=:event",
        parameters={"event": "EV-0000001"}, group_by="shift",
    ), scope, actor)
    assert result.status == "insufficient"
    group = result.payload["statistics"]["groups"][0]
    assert group["n"] == 1 and not group["adequate"] and group["significance"] is None
    cycle_scope = scope.model_copy(update={"metric_id": "cycle_time_mean"})
    result = verifier.execute(sql("SELECT AVG(cycle_time) AS actual FROM inspections"), cycle_scope, actor)
    group = result.payload["statistics"]["groups"][0]
    assert group["estimate"] == pytest.approx(result.payload["rows"][0]["actual"])
    assert group["ci95"][0] < group["estimate"] < group["ci95"][1]
    assert group["ci_method"] == "student_t"
    rework = verifier.execute(sql("SELECT SUM(rework) AS numerator FROM inspections"),
                             scope.model_copy(update={"metric_id": "rework_rate"}), actor)
    assert rework.payload["statistics"]["groups"][0]["numerator"] == rework.payload["rows"][0]["numerator"]


@pytest.mark.parametrize("settings,query,code", [
    ({"max_rows": 1}, "SELECT event_id FROM inspections", "row_budget"),
    ({"max_source_rows": 10}, "SELECT COUNT(*) FROM inspections", "source_row_budget"),
    ({"max_vm_steps": 1}, "SELECT COUNT(*) FROM inspections", "query_budget"),
    ({"timeout_seconds": 1e-12}, "SELECT COUNT(*) FROM inspections", "query_budget"),
])
def test_resource_budgets_are_errors_not_fabricated_evidence(business, settings, query, code):
    store, actor, scope = business
    with pytest.raises(DomainError) as error:
        Verifier(store, **settings).execute(sql(query), scope, actor)
    assert error.value.code == code


def test_nonfinite_sql_arithmetic_is_rejected(business):
    store, actor, scope = business
    with pytest.raises(DomainError) as error:
        Verifier(store).execute(sql("SELECT 1e308 * 1e308 AS invalid FROM inspections LIMIT 1"), scope, actor)
    assert error.value.code == "invalid_data"


def test_metadata_and_actual_scoped_work_orders(business):
    store, actor, scope = business
    verifier = Verifier(store)
    metadata = verifier.execute(ToolRequest(tool="metadata"), scope, actor)
    assert "defect_rate" in metadata.payload["metrics"]
    assert "event_id" in metadata.payload["tables"]["inspections"]
    assert "_dataset" not in metadata.payload["tables"]
    orders = verifier.execute(ToolRequest(tool="work_orders"), scope, actor)
    assert len(orders.payload["rows"]) == 3
    assert all(row["plant_id"] == "PLANT-A" and row["product_id"] == "WIDGET-A"
               for row in orders.payload["rows"])
    missing = verifier.execute(ToolRequest(tool="work_orders", work_order_id="WO-B-A-1"), scope, actor)
    assert missing.status == "insufficient" and missing.payload["rows"] == []


def test_data_watermark_mismatch_and_change_during_execution(business, monkeypatch):
    store, actor, scope = business
    verifier = Verifier(store)
    original = verifier._statistics

    def changing(*args):
        result = original(*args)
        with store.transaction() as db:
            db.execute("UPDATE settings SET value='2' WHERE key='data_revision'")
        return result

    monkeypatch.setattr(verifier, "_statistics", changing)
    with pytest.raises(DomainError) as error:
        verifier.execute(sql("SELECT COUNT(*) FROM inspections"), scope, actor)
    assert error.value.code == "data_changed"
    with pytest.raises(DomainError) as error:
        Verifier(store).execute(sql("SELECT COUNT(*) FROM inspections"), scope, actor)
    assert error.value.code == "data_changed"


def test_document_adapter_calls_current_authorized_drill_and_validates_citation(business):
    store, actor, scope = business
    text = "Synthetic investigation excerpt; no causal inference."
    citation = {
        "document_id": "DOC-1", "chunk_id": "CHUNK-1", "plant_id": "PLANT-A", "version": 1,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(), "text": text,
        "quote_start": 0, "quote_end": len(text),
    }
    seen = []

    class Adapter:
        def drill(self, document_id, chunk_id, current):
            seen.append((document_id, chunk_id, current))
            return citation.copy()

    request = ToolRequest(tool="document", document_id="DOC-1", chunk_id="CHUNK-1")
    result = Verifier(store, Adapter()).execute(request, scope, actor)
    assert seen == [("DOC-1", "CHUNK-1", actor)]
    assert result.citations == [citation]
    assert result.payload["untrusted_content"] is True
    for invalid in (text, {**citation, "plant_id": "PLANT-B"}, {**citation, "document_id": "OTHER"},
                    {**citation, "quote_end": 1}):
        with pytest.raises(DomainError):
            Verifier(store, lambda *_, value=invalid: value).execute(request, scope, actor)
    with pytest.raises(DomainError):
        Verifier(store).execute(request, scope, actor)


def test_sqlite_defense_in_depth_denies_raw_tables_writes_and_functions(business):
    store, _, scope = business
    verifier = Verifier(store)
    progress = verifier._progress()
    with closing(sqlite3.connect(":memory:")) as db:
        verifier._load_scope(db, scope, 1, progress)
        verifier._sandbox(db, progress)
        for query in ("SELECT * FROM main.sqlite_master", "SELECT rowid FROM inspections",
                      "DELETE FROM inspections", "PRAGMA query_only=OFF", "ATTACH ':memory:' AS x",
                      "SELECT randomblob(10) FROM inspections"):
            with pytest.raises(sqlite3.Error):
                db.execute(query)
