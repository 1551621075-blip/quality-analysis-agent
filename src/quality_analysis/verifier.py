from __future__ import annotations

import math
import re
import sqlite3
import statistics
import time
from collections import defaultdict
from contextlib import closing
from typing import Any

import sqlglot
from pydantic import ValidationError
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .business import DATASET_ID
from .schemas import Actor, DomainError, RetrievalHit, Scope, ToolRequest, ToolResult, digest
from .storage import Store

SCHEMAS = {
    "inspections": {
        "event_id": "TEXT", "plant_id": "TEXT", "product_id": "TEXT", "line_id": "TEXT",
        "day": "TEXT", "material_lot": "TEXT", "supplier_id": "TEXT", "shift": "TEXT",
        "defect": "INTEGER", "rework": "INTEGER", "cycle_time": "REAL",
    },
    "work_orders": {
        "order_id": "TEXT", "plant_id": "TEXT", "product_id": "TEXT", "line_id": "TEXT",
        "day": "TEXT", "issue": "TEXT", "resolution": "TEXT",
    },
    "material_lots": {
        "material_lot": "TEXT", "plant_id": "TEXT", "product_id": "TEXT", "line_id": "TEXT",
        "supplier_id": "TEXT", "received_day": "TEXT", "material_grade": "TEXT",
    },
}
METRICS = {
    "defect_rate": {"column": "defect", "aggregation": "sum / inspection count", "unit": "fraction"},
    "rework_rate": {"column": "rework", "aggregation": "sum / inspection count", "unit": "fraction"},
    "cycle_time_mean": {"column": "cycle_time", "aggregation": "mean", "unit": "seconds"},
}
_FUNCTIONS = {"count", "sum", "avg", "min", "max", "round", "abs", "coalesce", "nullif"}
_NODES = {
    "select", "from", "table", "tablealias", "identifier", "column", "star", "alias",
    "literal", "null", "boolean", "placeholder", "where", "group", "having", "order",
    "ordered", "limit", "distinct", "and", "or", "not", "paren", "eq", "neq", "gt",
    "gte", "lt", "lte", "is", "in", "between", "add", "sub", "mul", "div", "mod",
    "neg", "case", "if",
} | _FUNCTIONS
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _reject(message: str, code: str = "unsafe_sql") -> None:
    raise DomainError(code, message)


class _QueryBudget:
    """SQLite interruption plus cooperative deadline checks, not CPU preemption."""

    def __init__(self, max_vm_steps: int, timeout_seconds: float):
        self.deadline = time.monotonic() + timeout_seconds
        self.max_vm_steps = max_vm_steps
        self.interval = min(1000, max_vm_steps)
        self.steps = 0

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            _reject("Verification exceeded its execution deadline", "query_budget")
        return remaining

    def check(self) -> None:
        self.remaining()

    def progress(self) -> int:
        self.steps += self.interval
        return int(self.steps >= self.max_vm_steps or time.monotonic() >= self.deadline)

    def set_busy_timeout(self, db: sqlite3.Connection) -> None:
        # SQLite's busy handler does not invoke the VM progress callback. Refresh
        # its wait limit before each source read, using only the remaining budget.
        milliseconds = min(int(self.remaining() * 1000), 2**31 - 1)
        db.execute(f"PRAGMA busy_timeout={milliseconds}")


class Verifier:
    """Execute a deliberately small SQL language on physically scoped temporary copies.

    The optional documents adapter is an object with drill(document_id, chunk_id,
    actor), or a callable with that signature. It must enforce current document ACL,
    version and immutable citation integrity. DocumentStore returns a plant-bound
    envelope with one unchanged issued RetrievalHit in citations; trusted legacy
    adapters may instead return a flat auditable citation dictionary.
    """

    def __init__(
        self, store: Store, documents=None, *, max_rows: int = 200,
        max_source_rows: int = 50_000, max_vm_steps: int = 2_000_000,
        timeout_seconds: float = 3.0,
    ):
        if min(max_rows, max_source_rows, max_vm_steps) < 1 or not (
            math.isfinite(timeout_seconds) and timeout_seconds > 0
        ):
            raise ValueError("Verifier budgets must be positive and finite")
        self.store, self.documents = store, documents
        self.max_rows, self.max_source_rows = max_rows, max_source_rows
        self.max_vm_steps, self.timeout_seconds = max_vm_steps, timeout_seconds

    def _validate_sql(self, request: ToolRequest) -> tuple[exp.Select, str]:
        sql = request.sql or ""
        if "\x00" in sql:
            _reject("SQL NUL characters are not supported")
        try:
            # Tokenization distinguishes actual comments (including empty/trailing
            # ones) from the same markers inside SQLite string literals.
            if any(token.comments for token in sqlglot.tokenize(sql, read="sqlite")):
                _reject("SQL comments are not supported")
            statements = sqlglot.parse(sql, read="sqlite")
        except (SqlglotError, RecursionError) as exc:
            raise DomainError("unsafe_sql", "SQL could not be parsed within the expression budget") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            _reject("Exactly one SELECT statement is required")
        tree = statements[0]
        nodes = list(tree.walk())
        if len(nodes) > 400 or any(node.depth > 40 for node in nodes):
            _reject("SQL exceeds the expression complexity budget")
        if any(node.key not in _NODES for node in nodes):
            _reject("Only single-table SELECT, bounded expressions and approved aggregates are allowed")
        tables = list(tree.find_all(exp.Table))
        if len(tables) != 1:
            _reject("Exactly one approved table must be selected")
        table = tables[0]
        if any(value is not None and key not in {"this", "alias"}
               for key, value in table.args.items()):
            _reject("Table hints, qualification and modifiers are forbidden")
        if table.catalog or table.db or table.name not in SCHEMAS:
            _reject("Only unqualified approved table names are allowed")
        if table.alias and not _NAME.fullmatch(table.alias):
            _reject("Invalid table alias")
        alias_node = table.args.get("alias")
        if alias_node and alias_node.args.get("columns"):
            _reject("Table column alias lists are not allowed")
        aliases = {item.alias for item in tree.expressions if isinstance(item, exp.Alias)}
        normalized_aliases = {alias.lower() for alias in aliases}
        if len(normalized_aliases) != sum(isinstance(item, exp.Alias) for item in tree.expressions):
            _reject("Projection aliases must be unique ignoring case")
        if any(not _NAME.fullmatch(alias) for alias in aliases):
            _reject("Invalid projection alias")
        # SQLite can resolve TRUE/FALSE to projection aliases in WHERE, while
        # SQLGlot represents them as Boolean literals. Dropping the projections
        # for statistics would then silently change the observation cohort.
        if normalized_aliases & {"true", "false"}:
            _reject("Projection aliases cannot shadow Boolean literals")
        for node in nodes:
            if node.comments:
                _reject("Comments are not allowed")
            if isinstance(node, exp.If) and not isinstance(node.parent, exp.Case):
                _reject("Conditional function calls are not approved; use CASE")
            if isinstance(node, exp.Literal) and not node.is_string:
                try:
                    finite = math.isfinite(float(node.this))
                except (ValueError, OverflowError):
                    finite = False
                if not finite:
                    _reject("Nonfinite numeric SQL literals are forbidden")
            if isinstance(node, exp.Star) and any(node.args.values()):
                _reject("Modified stars are not allowed")
            if isinstance(node, exp.Column):
                if node.catalog or node.db or (
                    node.table and node.table != (table.alias or table.name)
                ):
                    _reject("Column qualification outside the selected table is forbidden")
                name = node.name
                if name == "*":
                    continue
                if name not in SCHEMAS[table.name]:
                    # Only ORDER BY / HAVING may refer to output aliases, never a WHERE
                    # that is reused to define the statistical cohort.
                    alias_context = node.find_ancestor(exp.Order, exp.Having)
                    if node.table or name not in aliases or alias_context is None:
                        _reject("Unknown or hidden column")
        placeholders = list(tree.find_all(exp.Placeholder))
        names = {node.this for node in placeholders}
        if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in names):
            _reject("Only named :parameter bindings are supported")
        if names != set(request.parameters):
            _reject("Bindings must match all and only the named SQL parameters", "sql_parameters")
        if any(isinstance(v, str) and len(v) > 2000 for v in request.parameters.values()):
            _reject("SQL parameter is too long", "sql_parameters")
        if any(isinstance(v, int) and not -(2**63) <= v < 2**63 for v in request.parameters.values()):
            _reject("SQL integer parameter exceeds SQLite range", "sql_parameters")
        limit = tree.args.get("limit")
        if limit:
            value = limit.expression
            if not isinstance(value, exp.Literal) or value.is_string:
                _reject("LIMIT must be a positive integer literal within the row budget")
            try:
                number = int(value.this)
            except ValueError:
                _reject("LIMIT must be an integer")
            if not 1 <= number <= self.max_rows:
                _reject("LIMIT exceeds the row budget", "row_budget")
        if request.group_by and table.name != "inspections":
            _reject("Metric grouping requires the inspections table", "invalid_tool_request")
        return tree, table.name

    def _progress(self) -> _QueryBudget:
        return _QueryBudget(self.max_vm_steps, self.timeout_seconds)

    @staticmethod
    def _scope_filter(scope: Scope, prefix: str = "") -> tuple[str, list[Any]]:
        clause = (
            f"{prefix}plant_id=? AND {prefix}product_id=? AND "
            f"{prefix}day>=? AND {prefix}day<?"
        )
        params: list[Any] = [scope.plant_id, scope.product_id,
                             scope.start_date.isoformat(), scope.end_date.isoformat()]
        if scope.line_ids:
            clause += f" AND {prefix}line_id IN ({','.join('?' for _ in scope.line_ids)})"
            params.extend(scope.line_ids)
        return clause, params

    def _load_scope(
        self, memory: sqlite3.Connection, scope: Scope, revision: int, progress: _QueryBudget,
    ) -> None:
        progress.check()
        memory.set_progress_handler(progress.progress, progress.interval)
        path = self.store.business_path
        if path.is_symlink() or not path.is_file():
            raise DomainError("business_missing", "Initialize the synthetic business dataset first", 409)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as source:
            source.execute("PRAGMA query_only=ON")
            source.execute("PRAGMA trusted_schema=OFF")
            source.set_progress_handler(progress.progress, progress.interval)
            source.execute("BEGIN")
            progress.set_busy_timeout(source)
            mark = source.execute("SELECT dataset_id,data_revision,synthetic FROM _dataset").fetchall()
            progress.check()
            if mark != [(DATASET_ID, revision, 1)]:
                raise DomainError("data_changed", "Business watermark does not match state revision", 409)
            clause, bindings = self._scope_filter(scope)
            total = 0
            for table, columns in SCHEMAS.items():
                fields = ",".join(columns)
                if table == "material_lots":
                    # Only metadata for lots referenced by inspections in the exact
                    # authorized date/product/line cohort, not every lot in the plant.
                    query = (
                        f"SELECT {fields} FROM material_lots WHERE material_lot IN "
                        f"(SELECT material_lot FROM inspections WHERE {clause}) "
                        "AND plant_id=? AND product_id=? LIMIT ?"
                    )
                    values = [*bindings, scope.plant_id, scope.product_id, self.max_source_rows + 1]
                else:
                    query = f"SELECT {fields} FROM {table} WHERE {clause} LIMIT ?"
                    values = [*bindings, self.max_source_rows + 1]
                progress.set_busy_timeout(source)
                rows = source.execute(query, values).fetchall()
                progress.check()
                self._validate_values(rows)
                progress.check()
                total += len(rows)
                if total > self.max_source_rows:
                    _reject("Scoped source exceeds the copy budget", "source_row_budget")
                declarations = ",".join(f"{name} {kind}" for name, kind in columns.items())
                memory.execute(f"CREATE TEMP TABLE {table}({declarations})")
                memory.executemany(
                    f"INSERT INTO temp.{table} VALUES({','.join('?' for _ in columns)})", rows,
                )
                progress.check()
            source.rollback()

    @staticmethod
    def _authorizer(action, first, second, database, trigger):
        if trigger is not None:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # SQLite's optimized COUNT(*) reports an empty column and no schema.
            # The sandbox's main DB is empty; these names exist only in temp.
            return sqlite3.SQLITE_OK if (
                first in SCHEMAS and (
                    (database == "temp" and (second == "" or second in SCHEMAS[first]))
                    or (database is None and second == "")
                )
            ) else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if (second or "").lower() in _FUNCTIONS else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    def _sandbox(self, db: sqlite3.Connection, progress: _QueryBudget) -> None:
        progress.check()
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 10_000)
        db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        db.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 50)
        db.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 1)
        db.setlimit(sqlite3.SQLITE_LIMIT_VDBE_OP, 20_000)
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        db.set_authorizer(self._authorizer)
        db.set_progress_handler(progress.progress, progress.interval)

    @staticmethod
    def _validate_values(rows) -> None:
        if any(isinstance(value, float) and not math.isfinite(value)
               for row in rows for value in row):
            _reject("Query or source produced nonfinite values", "invalid_data")

    @staticmethod
    def _interval(successes: int, n: int) -> list[float]:
        z = 1.959963984540054
        p, adjustment = successes / n, z * z / n
        center = (p + adjustment / 2) / (1 + adjustment)
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + adjustment)
        return [max(0.0, center - half), min(1.0, center + half)]

    def _group_stat(self, values: list[float], group: Any, scope: Scope) -> dict:
        n = len(values)
        adequate = n >= scope.min_group_n
        rate = scope.metric_id != "cycle_time_mean"
        numerator = int(sum(values)) if rate else None
        estimate = statistics.fmean(values)
        if rate:
            ci = self._interval(numerator, n)
            comparison_adequate = adequate and numerator >= 5 and n - numerator >= 5
        else:
            from scipy.stats import t

            half = float(t.ppf(0.975, n - 1)) * statistics.stdev(values) / math.sqrt(n) if n > 1 else None
            ci = [estimate - half, estimate + half] if half is not None else None
            comparison_adequate = adequate
        return {
            "group": group, "n": n, "denominator": n, "numerator": numerator,
            "estimate": estimate, "ci95": ci, "ci_method": "wilson" if rate else "student_t",
            "adequate": adequate, "comparison_adequate": comparison_adequate,
            "significance": None,
        }

    def _statistics(
        self, db, tree: exp.Select, request: ToolRequest, scope: Scope, progress: _QueryBudget,
    ) -> dict:
        # The model's projections/aggregates are display data, never trusted as
        # denominators. Recompute using actual observations matching its WHERE.
        progress.check()
        table = next(tree.find_all(exp.Table))
        qualifier = table.alias or table.name
        metric = METRICS[scope.metric_id]["column"]
        fields = list(dict.fromkeys([metric, request.group_by, "line_id", "day"]))
        fields = [field for field in fields if field is not None]
        cohort = exp.select(*(exp.column(field, table=qualifier) for field in fields)).from_(table.copy())
        if tree.args.get("where"):
            cohort.set("where", tree.args["where"].copy())
        rows = db.execute(cohort.sql(dialect="sqlite"), request.parameters).fetchmany(
            self.max_source_rows + 1
        )
        progress.check()
        if len(rows) > self.max_source_rows:
            _reject("Statistics exceed the observation budget", "source_row_budget")
        grouped, stratified = defaultdict(list), defaultdict(list)
        for index, row in enumerate(rows):
            if index % 256 == 0:
                progress.check()
            value = row[metric]
            key = row[request.group_by] if request.group_by else "all"
            grouped[key].append(value)
            if request.group_by in {"supplier_id", "material_lot"}:
                stratified[(key, row["line_id"], row["day"])].append(value)
        progress.check()
        if len(grouped) > self.max_rows or len(stratified) > self.max_rows:
            _reject("Statistics exceed the group row budget", "row_budget")
        groups, strata = [], []
        for key, values in sorted(grouped.items()):
            progress.check()
            groups.append(self._group_stat(values, key, scope))
            progress.check()
        for key, values in sorted(stratified.items()):
            progress.check()
            strata.append({
                **self._group_stat(values, key[0], scope), "line_id": key[1], "day": key[2],
            })
            progress.check()
        cautions = [
            "Original synthetic observations, not production measurements.",
            "Associations are not causal; line, time, material and shift may confound comparisons.",
            "Intervals assume independent observations; repeated lots/time can violate this assumption.",
            "No significance test is performed; exploratory multiple comparisons are not corrected.",
            "Statistics cover WHERE-filtered observations, ignoring display GROUP BY/HAVING/LIMIT.",
        ]
        if any(not g["comparison_adequate"] for g in groups):
            cautions.append("At least one group lacks adequate n or successes/failures for comparison.")
        result = {
            "metric_id": scope.metric_id, "unit": METRICS[scope.metric_id]["unit"],
            "group_by": request.group_by, "population_n": len(rows), "groups": groups,
            "stratified_groups": strata, "min_group_n": scope.min_group_n,
            "cohort_where": tree.args["where"].sql(dialect="sqlite") if tree.args.get("where") else None,
            "cautions": cautions,
        }
        if request.group_by == "supplier_id" and len(groups) > 1:
            matched = defaultdict(list)
            for item in strata:
                matched[(item["line_id"], item["day"])].append(item["estimate"])
            gaps = [max(v) - min(v) for v in matched.values() if len(v) == len(groups)]
            result["confounding_check"] = {
                "stratified_by": ["line_id", "day"],
                "marginal_gap": max(g["estimate"] for g in groups) - min(g["estimate"] for g in groups),
                "max_within_stratum_gap": max(gaps) if gaps else None,
                "matched_strata": len(gaps),
                "causal_conclusion": None,
            }
        progress.check()
        return result

    def _document(self, request: ToolRequest, scope: Scope, actor: Actor) -> ToolResult:
        if self.documents is None:
            raise DomainError("document_unavailable", "No authorized document adapter is configured", 503)
        adapter = getattr(self.documents, "drill", self.documents)
        if not callable(adapter):
            raise DomainError("document_unavailable", "Document adapter is not callable", 503)
        citation = adapter(request.document_id, request.chunk_id, actor)
        required = {"document_id", "chunk_id", "plant_id", "version", "content_hash", "text",
                    "quote_start", "quote_end"}
        if not isinstance(citation, dict) or not required <= citation.keys():
            raise DomainError("invalid_citation", "Document adapter must supply an auditable citation", 409)
        if citation["document_id"] != request.document_id or citation["chunk_id"] != request.chunk_id:
            raise DomainError("invalid_citation", "Document citation identity mismatch", 409)
        if citation["plant_id"] != scope.plant_id:
            raise DomainError("forbidden", "Document is outside the authorized plant scope", 403)
        start, end = citation["quote_start"], citation["quote_end"]
        if not (
            type(citation["version"]) is int and citation["version"] >= 1
            and isinstance(citation["content_hash"], str)
            and re.fullmatch(r"[0-9a-f]{64}", citation["content_hash"])
            and isinstance(citation["text"], str) and bool(citation["text"])
            and type(start) is int and type(end) is int and 0 <= start < end
            and end - start == len(citation["text"])
        ):
            raise DomainError("invalid_citation", "Malformed document citation", 409)
        citations = [citation]
        validator = getattr(self.documents, "validate_hit", None)
        if callable(validator) and "citations" not in citation:
            raise DomainError("invalid_citation", "Document drill omitted its issued citation", 409)
        if "citations" in citation:
            # Do not pass the enriched drill envelope to strict issued-hit validation.
            citations = citation["citations"]
            if not isinstance(citations, list) or len(citations) != 1 or not isinstance(citations[0], dict):
                raise DomainError("invalid_citation", "Document drill requires one issued citation", 409)
            try:
                hit = RetrievalHit.model_validate(citations[0]).model_dump(mode="json")
            except (ValidationError, TypeError, ValueError) as exc:
                raise DomainError("invalid_citation", "Malformed issued document citation", 409) from exc
            if any(citation.get(key) != value for key, value in hit.items()):
                raise DomainError("invalid_citation", "Document envelope and issued citation disagree", 409)
            if callable(validator) and validator(hit, actor) is not True:
                raise DomainError("invalid_citation", "Document adapter did not validate the issued hit", 409)
            citations = [hit]
        source = {key: citation[key] for key in (
            "document_id", "chunk_id", "plant_id", "version", "content_hash", "quote_start", "quote_end",
            "title", "source_version", "source_length", "source_manifest_hash", "provider_fingerprint",
            "index_revision",
        ) if key in citation}
        return ToolResult(
            status="ok", source="document",
            summary="Authorized historical document excerpt; not current metric or causal proof.",
            payload={"text": citation["text"], "untrusted_content": True, "provenance": {
                "document": source, "scope_binding": "plant_and_document_acl_only",
            }}, citations=citations,
        )

    def execute(self, request: ToolRequest, scope: Scope, actor: Actor) -> ToolResult:
        progress = self._progress()
        actor = self.store.current_actor(actor)
        progress.check()
        if scope.plant_id not in actor.plants:
            raise DomainError("forbidden", "Plant is outside the actor's current authorization", 403)
        # All current plant-authorized roles may use these read-only tools. Analysis
        # confirmation belongs to the caller; document mutation/ACL rules are unchanged.
        allowed = {
            "sql": {"tool", "sql", "parameters", "group_by"}, "metadata": {"tool"},
            "work_orders": {"tool", "work_order_id"}, "document": {"tool", "document_id", "chunk_id"},
        }[request.tool]
        if any(value not in (None, {}, []) and key not in allowed
               for key, value in request.model_dump().items()):
            raise DomainError("invalid_tool_request", "Arguments do not match the selected tool")
        tree, table = self._validate_sql(request) if request.tool == "sql" else (None, None)
        progress.check()
        revisions = self.store.revisions()
        progress.check()
        try:
            if request.tool == "document":
                result = self._document(request, scope, actor)
            else:
                with closing(sqlite3.connect(":memory:")) as db:
                    db.execute("PRAGMA trusted_schema=OFF")
                    db.row_factory = sqlite3.Row
                    self._load_scope(db, scope, revisions[0], progress)
                    self._sandbox(db, progress)
                    if request.tool == "metadata":
                        result = ToolResult(
                            status="ok", source="metadata", summary="Scoped synthetic schema and metric definitions.",
                            payload={"tables": SCHEMAS, "metrics": METRICS,
                                     "date_semantics": "start inclusive, end exclusive",
                                     "sql_policy": "One SELECT, one table, named bindings; no joins/CTEs/subqueries.",
                                     "material_lots_scope": "Only lots used by inspections in this scope"},
                        )
                    elif request.tool == "work_orders":
                        query, bindings = "SELECT * FROM work_orders", []
                        if request.work_order_id:
                            query += " WHERE order_id=?"
                            bindings.append(request.work_order_id)
                        query += " ORDER BY day,order_id"
                        rows = db.execute(query, bindings).fetchmany(self.max_rows + 1)
                        if len(rows) > self.max_rows:
                            _reject("Work orders exceed the result row budget", "row_budget")
                        result = ToolResult(
                            status="ok" if rows else "insufficient", source="work_orders",
                            summary="Scoped synthetic work orders; records are context, not causal proof.",
                            payload={"rows": [dict(row) for row in rows], "row_count": len(rows)},
                        )
                    else:
                        cursor = db.execute(tree.sql(dialect="sqlite"), request.parameters)
                        columns = [item[0] for item in cursor.description]
                        if len(columns) != len({column.lower() for column in columns}):
                            _reject("Duplicate result column names require unique aliases ignoring case")
                        rows = cursor.fetchmany(self.max_rows + 1)
                        self._validate_values(rows)
                        if len(rows) > self.max_rows:
                            _reject("SQL result exceeds the result row budget", "row_budget")
                        payload = {"columns": columns, "rows": [dict(row) for row in rows],
                                   "row_count": len(rows), "executed_sql": tree.sql(dialect="sqlite"),
                                   "parameters": request.parameters}
                        status = "ok" if rows else "insufficient"
                        if table == "inspections":
                            progress.check()
                            stats = self._statistics(db, tree, request, scope, progress)
                            progress.check()
                            payload["statistics"] = stats
                            if not stats["groups"] or any(not group["adequate"] for group in stats["groups"]):
                                status = "insufficient"
                        result = ToolResult(
                            status=status, source="sql", summary="Executed scoped synthetic SQLite SELECT; "
                            "computed statistics are descriptive, not evidence of causation.", payload=payload,
                        )
        except sqlite3.Error as exc:
            sqlite_code = getattr(exc, "sqlite_errorcode", 0)
            budget_error = (sqlite_code & 0xFF) in {
                sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            }
            code = "query_budget" if budget_error else "sql_execution"
            raise DomainError(code, "Query rejected or exceeded its bounded execution budget") from exc
        except (MemoryError, OverflowError) as exc:
            raise DomainError("query_budget", "Query exceeds SQLite resource limits") from exc
        progress.check()
        self.store.current_actor(actor)
        progress.check()
        if self.store.revisions() != revisions:
            raise DomainError("data_changed", "Data or document revision changed during verification", 409)
        result.payload["provenance"] = {
            **result.payload.get("provenance", {}),
            "index_revision": revisions[1], "actor_id": actor.actor_id, "acl_version": actor.acl_version,
        }
        if request.tool == "document":
            result.payload["provenance"].update(
                request_scope=scope.model_dump(mode="json"),
                request_scope_hash=digest(scope.model_dump(mode="json")),
            )
        else:
            result.payload["provenance"].update(
                scope=scope.model_dump(mode="json"), scope_hash=digest(scope.model_dump(mode="json")),
                data_revision=revisions[0], dataset_id=DATASET_ID, synthetic=True,
            )
        progress.check()
        return result
