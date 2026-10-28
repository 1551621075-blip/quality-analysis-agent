from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]
Grouping = Literal["line_id", "material_lot", "shift", "day", "supplier_id"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Role(StrEnum):
    OPERATOR = "operator"
    ANALYST = "analyst"
    ADMIN = "admin"


class Actor(StrictModel):
    actor_id: Identifier
    role: Role
    plants: list[Identifier] = Field(min_length=1, max_length=30)
    acl_version: int = Field(ge=1)


class Scope(StrictModel):
    plant_id: Identifier
    product_id: Identifier
    start_date: date
    end_date: date
    metric_id: Literal["defect_rate", "rework_rate", "cycle_time_mean"] = "defect_rate"
    line_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    min_group_n: int = Field(default=20, ge=5, le=500)

    @model_validator(mode="after")
    def ordered(self):
        days = (self.end_date - self.start_date).days
        if not 1 <= days <= 366:
            raise ValueError("时间范围必须为1至366天，结束日期不包含在内")
        if len(self.line_ids) != len(set(self.line_ids)):
            raise ValueError("产线不能重复")
        return self


class AnalyzeRequest(StrictModel):
    request_id: Identifier
    question: str = Field(min_length=2, max_length=2000)
    scope: Scope | None = None


class Confirmation(StrictModel):
    action_id: Identifier
    selected_sq_ids: list[Identifier] = Field(min_length=1, max_length=8)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class FollowupRequest(StrictModel):
    request_id: Identifier
    question: str = Field(min_length=2, max_length=2000)
    selected_sq_ids: list[Identifier] = Field(min_length=1, max_length=8)


class Subquestion(StrictModel):
    sq_id: Identifier
    title: str = Field(min_length=2, max_length=300)
    tier: Literal["core", "extended"]
    depends_on: list[Identifier] = Field(default_factory=list, max_length=7)
    group_by: Grouping | None = Field(default=None, description=(
        "当前SQ唯一授权的SQL分组字段；比较各组时必须保留该维度，即使称为总体对比。"
        "null仅表示不比较任何分组的单个汇总值，禁止GROUP BY。其他维度需独立SQ并单独授权。"))
    sources: list[Literal["sql", "metadata", "work_orders", "document"]] = Field(
        min_length=1, max_length=4
    )


class Plan(StrictModel):
    subquestions: list[Subquestion] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def valid_dag(self):
        nodes = {sq.sq_id: sq for sq in self.subquestions}
        if len(nodes) != len(self.subquestions) or not any(s.tier == "core" for s in nodes.values()):
            raise ValueError("SQ必须唯一且至少包含一个core")
        visiting, visited = set(), set()

        def walk(key):
            if key not in nodes:
                raise ValueError("未知依赖SQ")
            if key in visiting:
                raise ValueError("计划依赖成环")
            if key in visited:
                return
            visiting.add(key)
            for dep in nodes[key].depends_on:
                if dep not in nodes:
                    raise ValueError("未知依赖SQ")
                if nodes[key].tier == "core" and nodes[dep].tier != "core":
                    raise ValueError("core不能依赖extended")
                walk(dep)
            visiting.remove(key)
            visited.add(key)

        for key in nodes:
            if any(dep not in nodes for dep in nodes[key].depends_on):
                raise ValueError("未知依赖SQ")
            walk(key)
        return self

    def closure(self, selected: list[str]) -> list[str]:
        nodes = {sq.sq_id: sq for sq in self.subquestions}
        if not selected or len(selected) != len(set(selected)) or set(selected) - nodes.keys():
            raise DomainError("unknown_sq", "授权SQ不存在或重复")
        ordered = []

        def visit(key):
            for dependency in nodes[key].depends_on:
                visit(dependency)
            if key not in ordered:
                ordered.append(key)

        for key in selected:
            visit(key)
        return ordered


class ToolRequest(StrictModel):
    tool: Literal["sql", "metadata", "work_orders", "document"]
    sql: str | None = Field(default=None, max_length=6000)
    parameters: dict[str, str | int | float | None] = Field(default_factory=dict, max_length=30)
    group_by: Grouping | None = None
    document_id: Identifier | None = None
    chunk_id: Identifier | None = None
    work_order_id: Identifier | None = None

    @model_validator(mode="after")
    def finite(self):
        if any(isinstance(v, float) and not math.isfinite(v) for v in self.parameters.values()):
            raise ValueError("参数必须为有限数")
        if self.tool == "sql" and not self.sql:
            raise ValueError("sql工具需要查询")
        if self.tool == "document" and not (self.document_id and self.chunk_id):
            raise ValueError("原文下钻需要报告和切片ID")
        return self


class HypothesisDraft(StrictModel):
    hypothesis_key: Identifier
    statement: str = Field(min_length=2, max_length=500)
    tools: list[ToolRequest] = Field(min_length=1, max_length=3)


class Hypotheses(StrictModel):
    hypotheses: list[HypothesisDraft] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique(self):
        keys = [item.hypothesis_key for item in self.hypotheses]
        if len(keys) != len(set(keys)):
            raise ValueError("假设标识不得重复")
        return self


class Reflection(StrictModel):
    action: Literal["repair", "refine", "supplement", "no_action"]
    reason: str = Field(min_length=1, max_length=500)
    replacement: HypothesisDraft | None = None

    @model_validator(mode="after")
    def replacement_required(self):
        if (self.action == "no_action") != (self.replacement is None):
            raise ValueError("no_action不带替换假设，其他动作必须带替换假设")
        return self


class Intent(StrictModel):
    kind: Literal["quality_analysis", "out_of_scope", "clarify"]
    explanation: str = Field(min_length=1, max_length=400)


class ArtifactRef(StrictModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str


class SnapshotManifest(StrictModel):
    manifest_id: Identifier
    data_revision: int
    index_revision: int
    acl_version: int
    model_fingerprint: str
    scope_hash: str
    actor_id: Identifier


class ToolResult(StrictModel):
    status: Literal["ok", "insufficient", "refuted", "error"]
    summary: str
    source: str
    payload: dict[str, Any]
    citations: list[dict[str, Any]] = Field(default_factory=list)


class RetrievalHit(StrictModel):
    document_id: Identifier
    version: int
    chunk_id: Identifier
    content_hash: str
    text: str
    quote_start: int
    quote_end: int
    score: float
    channels: list[str]


class RetrievalResult(StrictModel):
    hits: list[RetrievalHit]
    low_prior: bool
    index_revision: int
    calibration_version: str


class DocumentInput(StrictModel):
    document_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    plant_id: Identifier
    shared_with: list[Identifier] = Field(default_factory=list, max_length=100)
    summaries: dict[str, str]
    text: str = Field(min_length=40, max_length=200000)

    @model_validator(mode="after")
    def five_summaries(self):
        if set(self.summaries) != {"phenomenon", "context", "hypothesis", "evidence", "action"}:
            raise ValueError("必须提供现象、背景、假设、证据、措施五个摘要入口")
        if any(not 2 <= len(value) <= 1000 for value in self.summaries.values()):
            raise ValueError("摘要长度必须为2至1000字符")
        return self
