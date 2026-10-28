from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from quality_analysis import verifier as module
from quality_analysis.business import example_scope, seed_business
from quality_analysis.schemas import DomainError, Scope, ToolRequest
from quality_analysis.storage import Store
from quality_analysis.verifier import Verifier


@pytest.fixture
def business(tmp_path):
    store = Store(tmp_path)
    seed_business(store)
    actor = store.authenticate(store.provision_user("analyst", "analyst", ["PLANT-A"]))
    return store, actor, example_scope()


def sql(text, **kwargs):
    return ToolRequest(tool="sql", sql=text, **kwargs)


def test_literal_comment_marker_in_schema_valid_line_matches_named_binding(business):
    store, actor, scope = business
    with closing(sqlite3.connect(store.business_path)) as db:
        db.execute("UPDATE inspections SET line_id=? WHERE line_id=?", ("LINE--1", "LINE-1"))
        db.commit()
    scope = Scope.model_validate({**scope.model_dump(), "line_ids": ["LINE--1"]})
    verifier = Verifier(store)
    query = (
        "SELECT line_id,COUNT(*) AS n FROM inspections "
        "WHERE line_id=:line GROUP BY line_id"
    )
    bound = verifier.execute(sql(query, group_by="line_id", parameters={"line": "LINE--1"}),
                             scope, actor)
    literal = verifier.execute(sql(query.replace(":line", "'LINE--1'"), group_by="line_id"),
                               scope, actor)
    assert literal.status == bound.status == "ok"
    assert literal.payload["rows"] == bound.payload["rows"] == [{"line_id": "LINE--1", "n": 3100}]
    assert literal.payload["statistics"]["population_n"] == 3100
    assert literal.payload["statistics"]["groups"] == bound.payload["statistics"]["groups"]
    assert literal.payload["parameters"] == {}


@pytest.mark.parametrize("text", [
    "--", "/*", "*/", "/* not a comment */",
    "quote'--/* */", "backslash\\' /* */ --", "line one\n/* line two */ --",
    "; SELECT 'still literal' -- /* */",
])
def test_comment_markers_inside_sql_string_literals_are_data(business, text):
    store, actor, scope = business
    with closing(sqlite3.connect(store.business_path)) as db:
        # Use a scoped existing order without relying on a particular fixture ID.
        order_id = db.execute(
            "SELECT order_id FROM work_orders WHERE plant_id=? AND product_id=? LIMIT 1",
            (scope.plant_id, scope.product_id),
        ).fetchone()[0]
        db.execute("UPDATE work_orders SET issue=? WHERE order_id=?", (text, order_id))
        db.commit()
    literal = "'" + text.replace("'", "''") + "'"
    verifier = Verifier(store)
    result = verifier.execute(sql(
        f"SELECT order_id,issue,{literal} AS literal_value FROM work_orders WHERE issue={literal}",
    ), scope, actor)
    assert result.status == "ok"
    assert result.payload["rows"] == [{"order_id": order_id, "issue": text, "literal_value": text}]
    assert result.payload["parameters"] == {}
    with pytest.raises(DomainError) as error:
        verifier.execute(sql(
            f"SELECT order_id FROM work_orders WHERE issue={literal}", parameters={"extra": text},
        ), scope, actor)
    assert error.value.code == "sql_parameters"


@pytest.mark.parametrize("query", [
    "-- leading\nSELECT COUNT(*) FROM inspections",
    "/* leading */ SELECT COUNT(*) FROM inspections",
    "SELECT /* middle */ COUNT(*) FROM inspections",
    "SELECT COUNT(*) FROM inspections -- trailing",
    "SELECT COUNT(*) FROM inspections --",
    "SELECT COUNT(*) FROM inspections /**/",
    "SELECT COUNT(*) FROM inspections; --",
    "SELECT COUNT(*) FROM inspections; /**/",
    "SELECT COUNT(*) FROM inspections /* unterminated",
    "SELECT COUNT(*) FROM inspections */",
    "SELECT 'safe -- /* */' FROM inspections -- real comment",
    "SELECT 'safe''-- /* */' FROM inspections /* real comment */",
    "SELECT 'safe -- /* */' FROM inspections; SELECT COUNT(*) FROM inspections",
    "SELECT 'literal\x00value' FROM inspections",
    "SELECT COUNT(*) FROM inspections\x00",
])
def test_actual_sql_comments_nul_and_multiple_statements_remain_rejected(business, query):
    store, actor, scope = business
    with pytest.raises(DomainError) as error:
        Verifier(store).execute(sql(query), scope, actor)
    assert error.value.code == "unsafe_sql"


@pytest.mark.parametrize("alias", ["true", "TRUE", "TrUe", "false", "FALSE", "FaLsE"])
def test_boolean_alias_cannot_change_recomputed_where_cohort(business, alias):
    store, actor, scope = business
    request = sql(
        f'SELECT defect AS "{alias}", COUNT(*) AS n, AVG(defect) AS rate '
        f"FROM inspections WHERE {alias}=1"
    )
    with pytest.raises(DomainError) as error:
        Verifier(store).execute(request, scope, actor)
    assert error.value.code == "unsafe_sql"


@pytest.mark.parametrize("literal,expected_n", [("TRUE", 6200), ("FALSE", 0)])
def test_boolean_literals_without_shadowing_preserve_cohort(business, literal, expected_n):
    store, actor, scope = business
    result = Verifier(store).execute(
        sql(f"SELECT COUNT(*) AS n FROM inspections WHERE {literal}"), scope, actor,
    )
    assert result.payload["rows"] == [{"n": expected_n}]
    assert result.payload["statistics"]["population_n"] == expected_n


@pytest.mark.parametrize("query", [
    "SELECT COUNT(*) AS n, SUM(defect) AS N FROM inspections",
    "SELECT defect AS metric, cycle_time AS METRIC FROM inspections LIMIT 1",
    "SELECT issue AS text, resolution AS TEXT FROM work_orders LIMIT 1",
    "SELECT defect, cycle_time AS DEFECT FROM inspections LIMIT 1",
    "SELECT *, cycle_time AS DEFECT FROM inspections LIMIT 1",
])
def test_case_colliding_sql_labels_never_replace_values(business, query):
    store, actor, scope = business
    with pytest.raises(DomainError) as error:
        Verifier(store).execute(sql(query), scope, actor)
    assert error.value.code == "unsafe_sql"


def test_noncolliding_result_labels_preserve_sql_and_metric_values(business):
    store, actor, scope = business
    result = Verifier(store).execute(sql(
        "SELECT COUNT(*) AS N, SUM(defect) AS defects FROM inspections",
    ), scope, actor)
    assert result.payload["rows"] == [{"N": 6200, "defects": 935}]
    group = result.payload["statistics"]["groups"][0]
    assert (group["denominator"], group["numerator"]) == (6200, 935)


def test_source_lock_wait_uses_query_budget_not_sqlite_default(business):
    store, actor, scope = business
    with closing(sqlite3.connect(store.business_path)) as locker:
        locker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            with pytest.raises(DomainError) as error:
                Verifier(store, timeout_seconds=0.03).execute(
                    sql("SELECT COUNT(*) FROM inspections"), scope, actor,
                )
        finally:
            locker.rollback()
    assert error.value.code == "query_budget"
    # Generous scheduling tolerance, still far below SQLite's old five-second wait.
    assert time.monotonic() - started < 1.0


def test_source_busy_timeout_uses_remaining_not_initial_budget(business, monkeypatch):
    store, actor, scope = business
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    verifier = Verifier(store, timeout_seconds=0.1)
    original_validate = verifier._validate_sql
    original_connect = sqlite3.connect
    statements = []

    def validate(request):
        result = original_validate(request)
        clock[0] = 0.04
        return result

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        if args[0] == store.business_path.as_uri() + "?mode=ro":
            assert kwargs["timeout"] == 0
            db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(verifier, "_validate_sql", validate)
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    result = verifier.execute(sql("SELECT COUNT(*) AS n FROM inspections"), scope, actor)
    assert result.status == "ok"
    timeouts = [int(statement.split("=")[1]) for statement in statements
                if statement.startswith("PRAGMA busy_timeout=")]
    assert len(timeouts) == 4  # Watermark and every scoped source table.
    assert all(50 <= value <= 60 for value in timeouts)


def test_python_group_processing_cannot_return_success_after_deadline(business, monkeypatch):
    store, actor, scope = business
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    verifier = Verifier(store, timeout_seconds=0.1)
    original = verifier._group_stat
    seen = []

    def slow_group(*args):
        result = original(*args)
        seen.append(result["group"])
        clock[0] = 0.2
        return result

    monkeypatch.setattr(verifier, "_group_stat", slow_group)
    with pytest.raises(DomainError) as error:
        verifier.execute(sql(
            "SELECT line_id, COUNT(*) AS n FROM inspections GROUP BY line_id",
            group_by="line_id",
        ), scope, actor)
    assert error.value.code == "query_budget"
    assert seen == ["LINE-1"]  # Stop at the Python boundary before another group.


def test_expired_parse_budget_stops_before_loading_source(business, monkeypatch):
    store, actor, scope = business
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    verifier = Verifier(store, timeout_seconds=0.1)
    original = verifier._validate_sql

    def slow_parse(request):
        result = original(request)
        clock[0] = 0.2
        return result

    def forbidden_load(*_):
        pytest.fail("Expired parsing must not start source loading")

    monkeypatch.setattr(verifier, "_validate_sql", slow_parse)
    monkeypatch.setattr(verifier, "_load_scope", forbidden_load)
    with pytest.raises(DomainError) as error:
        verifier.execute(sql("SELECT COUNT(*) FROM inspections"), scope, actor)
    assert error.value.code == "query_budget"


def test_final_revision_check_cannot_return_success_after_deadline(business, monkeypatch):
    store, actor, scope = business
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = store.revisions
    calls = []

    def revisions():
        result = original()
        calls.append(result)
        if len(calls) == 2:
            clock[0] = 0.2
        return result

    monkeypatch.setattr(store, "revisions", revisions)
    with pytest.raises(DomainError) as error:
        Verifier(store, timeout_seconds=0.1).execute(ToolRequest(tool="metadata"), scope, actor)
    assert error.value.code == "query_budget"
    assert len(calls) == 2


def test_expired_empty_scope_is_rejected_without_vm_progress(business, monkeypatch):
    store, actor, scope = business
    scope = scope.model_copy(update={
        "start_date": scope.start_date.replace(month=3),
        "end_date": scope.end_date.replace(month=4),
    })
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    verifier = Verifier(store, timeout_seconds=0.1)
    original = verifier._load_scope

    def load(*args):
        original(*args)
        clock[0] = 0.2

    monkeypatch.setattr(verifier, "_load_scope", load)
    with pytest.raises(DomainError) as error:
        verifier.execute(sql("SELECT COUNT(*) FROM inspections"), scope, actor)
    assert error.value.code == "query_budget"


def citation():
    text = "Synthetic investigation excerpt; no causal inference."
    return {
        "document_id": "DOC-1", "chunk_id": "CHUNK-1", "plant_id": "PLANT-A", "version": 1,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(), "text": text,
        "quote_start": 0, "quote_end": len(text),
    }


def test_document_adapter_cannot_return_success_after_deadline(business, monkeypatch):
    store, actor, scope = business
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow_document(*_):
        clock[0] = 0.2
        return citation()

    with pytest.raises(DomainError) as error:
        Verifier(store, slow_document, timeout_seconds=0.1).execute(
            ToolRequest(tool="document", document_id="DOC-1", chunk_id="CHUNK-1"), scope, actor,
        )
    assert error.value.code == "query_budget"


@pytest.mark.parametrize("role", ["operator", "analyst", "admin"])
@pytest.mark.parametrize("tool", ["sql", "metadata", "work_orders", "document"])
def test_current_plant_authorized_roles_can_use_readonly_tools(business, role, tool):
    store, _, scope = business
    actor = store.authenticate(store.provision_user("reader", role, ["PLANT-A"]))
    seen = []

    def document(document_id, chunk_id, current):
        seen.append((document_id, chunk_id, current))
        return citation()

    request = {
        "sql": sql("SELECT COUNT(*) AS n FROM inspections"),
        "metadata": ToolRequest(tool="metadata"),
        "work_orders": ToolRequest(tool="work_orders"),
        "document": ToolRequest(tool="document", document_id="DOC-1", chunk_id="CHUNK-1"),
    }[tool]
    result = Verifier(store, document).execute(request, scope, actor)
    assert result.status == "ok"
    assert result.payload["provenance"]["actor_id"] == actor.actor_id
    if tool == "document":
        assert seen == [("DOC-1", "CHUNK-1", actor)]


def test_operator_still_cannot_escape_scope_write_or_use_stale_acl(business):
    store, _, scope = business
    actor = store.authenticate(store.provision_user("operator", "operator", ["PLANT-A"]))
    verifier = Verifier(store)
    for query in ("DELETE FROM inspections", "UPDATE inspections SET defect=0",
                  "ATTACH ':memory:' AS outside", "SELECT * FROM main.inspections"):
        with pytest.raises(DomainError) as error:
            verifier.execute(sql(query), scope, actor)
        assert error.value.code == "unsafe_sql"
    with pytest.raises(DomainError) as error:
        verifier.execute(sql("SELECT COUNT(*) FROM inspections"),
                         scope.model_copy(update={"plant_id": "PLANT-B"}), actor)
    assert error.value.code == "forbidden"
    with store.transaction() as db:
        db.execute("UPDATE users SET acl_version=acl_version+1 WHERE actor_id='operator'")
    with pytest.raises(DomainError) as error:
        verifier.execute(sql("SELECT COUNT(*) FROM inspections"), scope, actor)
    assert error.value.code == "acl_changed"
    with store.transaction() as db:
        db.execute("UPDATE users SET active=0 WHERE actor_id='operator'")
    with pytest.raises(DomainError) as error:
        verifier.execute(sql("SELECT COUNT(*) FROM inspections"), scope, actor)
    assert error.value.code == "unauthorized"


def test_operator_document_access_still_requires_authorized_adapter(business):
    store, _, scope = business
    actor = store.authenticate(store.provision_user("operator", "operator", ["PLANT-A"]))

    def denied_document(*_):
        raise DomainError("forbidden", "Document is not shared with this actor", 403)

    with pytest.raises(DomainError) as error:
        Verifier(store, denied_document).execute(
            ToolRequest(tool="document", document_id="DOC-1", chunk_id="CHUNK-1"), scope, actor,
        )
    assert error.value.code == "forbidden"
