from __future__ import annotations

import copy
import json
import time
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context
from pydantic import ValidationError
from sqlglot import exp, parse
from sqlglot.errors import ParseError, TokenError

from .schemas import (
    Actor,
    AnalyzeRequest,
    Confirmation,
    DomainError,
    FollowupRequest,
    Hypotheses,
    Intent,
    Plan,
    Reflection,
    Scope,
    SnapshotManifest,
    ToolResult,
    canonical,
    digest,
)
from .storage import Store, now

NODES = ["context_resolver", "intent", "cohort", "cache_guard", "prior_check", "plan",
         "confirm", "hypothesizer", "verifier", "hop_close", "loop_ctrl", "reflection",
         "report_renderer"]
MAX_CALLS, MAX_TOOLS, MAX_TOKENS, MAX_SECONDS = 20, 24, 24000, 300


class GraphState(TypedDict, total=False):
    session_id: str
    round_id: str
    status: str
    mode: str
    request: dict
    actor: dict
    manifest: dict
    plan: dict
    plan_fingerprint: str
    authorized: list[str]
    queue: list[str]
    sq_index: int
    hypotheses: list[dict]
    pending_hypotheses: list[dict]
    evidence_ids: list[str]
    hop_evidence_ids: list[str]
    guard_ids: list[str]
    claim_ids: list[str]
    revision_ids: list[str]
    decisions: list[dict]
    reflection_used: list[str]
    calls: int
    tools: int
    tokens: int
    elapsed: float
    trace: list[str]
    prior_ref: dict
    report_ref: dict
    last_error: dict | None
    cache_key: str
    next_node: str
    state_version: int
    worker_id: str | None
    reflection_contexts: dict[str, dict]


def new_id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex[:24]


class AnalysisService:
    def __init__(self, store: Store, provider, documents, verifier):
        self.store, self.provider, self.documents, self.verifier = store, provider, documents, verifier
        graph = StateGraph(GraphState)
        for name in NODES:
            graph.add_node(name, self._wrapper(name))
        graph.add_conditional_edges(START, lambda s: s.get("next_node", "context_resolver"),
                                    {name: name for name in NODES} | {"end": END})
        for name in NODES:
            graph.add_conditional_edges(name, lambda s: s["next_node"],
                                        {target: target for target in NODES} | {"end": END})
        self.graph = graph.compile()

    def _owned(self, session_id: str, actor: Actor):
        self.store.current_actor(actor)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None or row["owner"] != actor.actor_id:
            raise DomainError("session_missing", "分析会话不存在", 404)
        state = json.loads(row["state_json"])
        scope = state["request"].get("scope")
        if scope and scope["plant_id"] not in actor.plants:
            raise DomainError("forbidden", "当前权限不包含分析工厂", 403)
        return row, state

    def get(self, session_id: str, actor: Actor) -> dict:
        _, state = self._owned(session_id, actor)
        return self._view(state, actor)

    def _view(self, state, actor):
        self.store.current_actor(actor)
        scope = state["request"].get("scope")
        if scope and scope["plant_id"] not in actor.plants:
            raise DomainError("forbidden", "当前权限不包含分析工厂", 403)
        output = {k: state.get(k) for k in ("session_id", "round_id", "status", "plan",
                   "plan_fingerprint", "authorized", "trace", "last_error", "calls", "tools", "tokens")}
        output["request"] = state["request"]
        output["manifest"] = state.get("manifest")
        output["provider"] = self.provider.fingerprint
        if state.get("manifest") and not self._manifest_current(state, actor):
            output["plan"] = None
            output["plan_fingerprint"] = None
        if state.get("report_ref"):
            if self._manifest_current(state, actor):
                output["report"] = self.store.read_artifact(state["report_ref"])
                output["report"]["stale"] = False
            else:
                output["report"] = {"stale": True, "sections": [], "unexecuted": [],
                                    "reason": "数据、报告授权或版本已变化；旧报告内容不再展示"}
        return output

    def list_sessions(self, actor: Actor) -> list[dict]:
        self.store.current_actor(actor)
        with self.store.connect() as db:
            rows = db.execute("SELECT session_id FROM sessions WHERE owner=? ORDER BY created_at DESC LIMIT 100",
                              (actor.actor_id,)).fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.get(row["session_id"], actor))
            except DomainError as exc:
                if exc.code != "forbidden":
                    raise
        return results

    def _new_state(self, request: AnalyzeRequest, actor: Actor, session_id: str) -> GraphState:
        return GraphState(session_id=session_id, round_id=new_id("round"), status="preparing",
            mode="prepare", request=request.model_dump(mode="json"), actor=actor.model_dump(mode="json"),
            authorized=[], queue=[], sq_index=0, hypotheses=[], pending_hypotheses=[],
            evidence_ids=[], hop_evidence_ids=[], guard_ids=[], claim_ids=[], revision_ids=[], decisions=[],
            reflection_used=[], calls=0, tools=0, tokens=0, elapsed=0, trace=[],
            last_error=None, next_node="context_resolver", state_version=0, worker_id=None,
            reflection_contexts={})

    def analyze(self, request: AnalyzeRequest, actor: Actor) -> dict:
        self.store.current_actor(actor)
        if request.scope and request.scope.plant_id not in actor.plants:
            raise DomainError("forbidden", "不能分析未授权工厂", 403)
        hashed = digest(request.model_dump(mode="json"))
        with self.store.transaction() as db:
            prior = db.execute("SELECT * FROM sessions WHERE owner=? AND request_id=?",
                               (actor.actor_id, request.request_id)).fetchone()
            if prior:
                if prior["payload_hash"] != hashed:
                    raise DomainError("request_conflict", "请求ID已用于其他内容", 409)
                session_id = prior["session_id"]
                state = None
            else:
                session_id = new_id("analysis")
                state = self._new_state(request, actor, session_id)
                db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
                           (session_id, actor.actor_id, request.request_id, hashed,
                            canonical(state["request"]), state["status"], canonical(state), 0, now()))
                db.execute("INSERT INTO rounds VALUES(?,?,?,?,?,?,?,?)",
                           (state["round_id"], session_id, 1, state["status"], "{}", "[]", canonical(state), now()))
                self.store.audit(db, actor.actor_id, "analysis_requested", {"request_hash": hashed}, session_id)
        if state is not None:
            self._run(state)
        return self.get(session_id, actor)

    def _action_start(self, db, actor, action_id, payload):
        hashed = digest(payload)
        row = db.execute("SELECT * FROM actions WHERE actor_id=? AND action_id=?",
                         (actor.actor_id, action_id)).fetchone()
        if row:
            if row["payload_hash"] != hashed:
                raise DomainError("action_conflict", "操作ID对应不同内容", 409)
            response = json.loads(row["response_json"])
            if response == {"status": "in_progress"}:
                response = self._action_response(db, actor, action_id)
                if response is None:
                    return {"status": "in_progress"}
                db.execute("UPDATE actions SET response_json=? WHERE actor_id=? AND action_id=?",
                           (canonical(response), actor.actor_id, action_id))
            if response.get("manifest") and not self._manifest_current(response, actor):
                raise DomainError("snapshot_changed", "操作回执的授权或数据快照已失效，不能重放旧内容", 409)
            return response
        db.execute("INSERT INTO actions VALUES(?,?,?,?)",
                   (actor.actor_id, action_id, hashed, canonical({"status": "in_progress"})))
        return None

    def _bind_action(self, db, actor, action_id, state):
        db.execute("INSERT INTO action_rounds VALUES(?,?,?,?,?,?,NULL)",
                   (actor.actor_id, action_id, state["session_id"], state["round_id"],
                    state["mode"], state["state_version"]))

    def _action_response(self, db, actor, action_id):
        self.store.current_actor(actor)
        binding = db.execute("SELECT * FROM action_rounds WHERE actor_id=? AND action_id=?",
                             (actor.actor_id, action_id)).fetchone()
        if binding is None:
            # Legacy placeholders cannot safely be attached to the latest round.
            raise DomainError("action_unrecoverable", "旧操作缺少轮次绑定，不能推测回执", 409)
        if binding["outcome_json"] is None:
            return None
        state = json.loads(binding["outcome_json"])
        if state.get("manifest") and not self._manifest_current(state, actor):
            raise DomainError("snapshot_changed", "操作回执的授权或数据快照已失效", 409)
        return self._view(state, actor)

    def _action_finish(self, actor, action_id, response):
        with self.store.transaction() as db:
            # Use the action's durable outcome, never a newer session/followup.
            response = self._action_response(db, actor, action_id)
            if response is None:
                return {"status": "in_progress"}
            db.execute("UPDATE actions SET response_json=? WHERE actor_id=? AND action_id=?",
                       (canonical(response), actor.actor_id, action_id))
        return response

    def confirm(self, session_id: str, confirmation: Confirmation, actor: Actor) -> dict:
        self._owned(session_id, actor)
        with self.store.transaction() as db:
            prior = self._action_start(db, actor, confirmation.action_id,
                                      {"operation": "confirm", "session": session_id,
                                       **confirmation.model_dump()})
            if prior is not None:
                return prior
            state = self._control_state(db, session_id)
            if state["status"] != "awaiting_confirmation":
                raise DomainError("not_waiting", "会话当前不等待计划确认", 409)
            if confirmation.plan_fingerprint != state["plan_fingerprint"]:
                raise DomainError("plan_changed", "确认指纹不匹配", 409)
            plan = Plan.model_validate(state["plan"])
            selected = plan.closure(confirmation.selected_sq_ids)
            if any(s.tier != "core" for s in plan.subquestions if s.sq_id in selected):
                raise DomainError("first_round_core", "首轮只能执行core，extended须后续授权", 409)
            if not self._manifest_current(state, actor):
                raise DomainError("snapshot_changed", "数据、报告、权限或模型已变化，请重新发起分析", 409)
            state.update(mode="execute", status="running", authorized=selected, queue=selected,
                         next_node="hypothesizer")
            self._save_locked(db, state, "confirmed")
            self._bind_action(db, actor, confirmation.action_id, state)
            self.store.audit(db, actor.actor_id, "plan_confirmed", {"authorized": selected}, session_id)
        self._run(state)
        return self._action_finish(actor, confirmation.action_id, None)

    def followup(self, session_id: str, request: FollowupRequest, actor: Actor) -> dict:
        self._owned(session_id, actor)
        with self.store.transaction() as db:
            prior = self._action_start(db, actor, request.request_id,
                                      {"operation": "followup", "session": session_id, **request.model_dump()})
            if prior is not None:
                return prior
            old = self._control_state(db, session_id)
            if old["status"] not in {"completed", "limited"}:
                raise DomainError("not_finished", "只有已结束轮次可发起追问", 409)
            if not self._manifest_current(old, actor):
                raise DomainError("snapshot_changed", "原分析快照已失效，须创建新分析", 409)
            selected = Plan.model_validate(old["plan"]).closure(request.selected_sq_ids)
            state = self._new_state(AnalyzeRequest(request_id=request.request_id,
                question=request.question, scope=Scope.model_validate(old["request"]["scope"])), actor, session_id)
            ordinal = db.execute("SELECT MAX(ordinal)+1 FROM rounds WHERE session_id=?", (session_id,)).fetchone()[0]
            state.update(mode="execute", status="running", plan=old["plan"],
                         plan_fingerprint=old["plan_fingerprint"], manifest=self._manifest(state, actor),
                         authorized=selected, queue=selected, next_node="hypothesizer",
                         prior_ref=old.get("prior_ref"), calls=old["calls"], tools=old["tools"],
                         tokens=old["tokens"], elapsed=old["elapsed"], state_version=old["state_version"])
            # Dependencies are explicitly authorized and re-executed with NEW evidence identities.
            db.execute("INSERT INTO rounds VALUES(?,?,?,?,?,?,?,?)",
                       (state["round_id"], session_id, ordinal, "running", canonical(state["manifest"]),
                        canonical(selected), canonical(state), now()))
            self._save_locked(db, state, "followup_authorized")
            self._bind_action(db, actor, request.request_id, state)
            self.store.audit(db, actor.actor_id, "followup_authorized", {"closure": selected}, session_id)
        self._run(state)
        return self._action_finish(actor, request.request_id, None)

    def _manifest(self, state, actor):
        data, index = self.store.revisions()
        return SnapshotManifest(manifest_id=new_id("snapshot"), data_revision=data,
            index_revision=index, acl_version=actor.acl_version, model_fingerprint=self.provider.fingerprint,
            scope_hash=digest(state["request"]["scope"]), actor_id=actor.actor_id).model_dump()

    def _manifest_current(self, state, actor):
        try:
            self.store.current_actor(actor)
        except DomainError:
            return False
        if not state.get("manifest"):
            return False
        current = self._manifest(state, actor)
        return all(current[k] == value for k, value in state["manifest"].items() if k != "manifest_id")

    def _control_state(self, db, session_id):
        active = db.execute("SELECT expires_at FROM leases WHERE session_id=?", (session_id,)).fetchone()
        if active and active["expires_at"] > time.time():
            raise DomainError("analysis_busy", "会话已有执行者", 409)
        row = db.execute("SELECT state_json,version FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        state = json.loads(row["state_json"])
        state.update(state_version=row["version"], worker_id=None)
        return state

    def _assert_state(self, db, state):
        row = db.execute("SELECT version FROM sessions WHERE session_id=?", (state["session_id"],)).fetchone()
        if row is None or row["version"] != state.get("state_version", 0):
            raise DomainError("stale_worker", "执行状态已被更新，旧执行者不能继续", 409)
        if state.get("worker_id"):
            lease = db.execute("SELECT * FROM leases WHERE session_id=?", (state["session_id"],)).fetchone()
            if not lease or lease["owner"] != state["worker_id"] or lease["expires_at"] <= time.time():
                raise DomainError("stale_worker", "执行租约已失效，不能继续写入或调用", 409)

    def _save_locked(self, db, state, node):
        self._assert_state(db, state)
        version = state.get("state_version", 0) + 1
        state["state_version"] = version
        db.execute("UPDATE sessions SET status=?,state_json=?,version=? WHERE session_id=?",
                   (state["status"], canonical(state), version, state["session_id"]))
        db.execute("UPDATE rounds SET status=?,manifest_json=?,authorization_json=?,state_json=? WHERE round_id=?",
                   (state["status"], canonical(state.get("manifest", {})), canonical(state["authorized"]),
                    canonical(state), state["round_id"]))
        db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                   (state["session_id"], version, node, canonical(state), now()))
        if state.get("next_node") == "end":
            db.execute("UPDATE action_rounds SET outcome_json=? WHERE session_id=? AND round_id=? "
                       "AND mode=? AND start_version<=? AND outcome_json IS NULL",
                       (canonical(state), state["session_id"], state["round_id"], state["mode"], version))

    def _checkpoint(self, state, node):
        with self.store.transaction() as db:
            self._save_locked(db, state, node)

    def _run(self, state):
        lease = new_id("worker")
        with self.store.transaction() as db:
            self._assert_state(db, state)
            row = db.execute("SELECT * FROM leases WHERE session_id=?", (state["session_id"],)).fetchone()
            if row and row["expires_at"] > time.time():
                raise DomainError("analysis_busy", "会话已有执行者", 409)
            db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)",
                       (state["session_id"], lease, time.time() + MAX_SECONDS + 240))
            state["worker_id"] = lease
        try:
            # Do not inherit optional tracing uploads from the host environment.
            with tracing_context(enabled=False):
                self.graph.invoke(state, {"recursion_limit": 150, "callbacks": []})
        finally:
            with self.store.transaction() as db:
                db.execute("DELETE FROM leases WHERE session_id=? AND owner=?", (state["session_id"], lease))

    def resume(self, session_id: str, action_id: str, actor: Actor) -> dict:
        self._owned(session_id, actor)
        with self.store.transaction() as db:
            prior = self._action_start(db, actor, action_id, {"operation": "resume", "session": session_id})
            if prior is not None:
                return prior
            state = self._control_state(db, session_id)
            unknown_tool = db.execute("SELECT 1 FROM tool_calls WHERE session_id=? AND status='started'",
                                      (session_id,)).fetchone()
            if unknown_tool:
                raise DomainError("tool_unknown", "工具结果未持久化，禁止自动重试；请创建新分析", 409)
            unresolved = db.execute("SELECT 1 FROM calls WHERE session_id=? AND status IN ('started','unknown')",
                                    (session_id,)).fetchone()
            if unresolved:
                raise DomainError("provider_unknown", "存在未知推理结果，须先对账", 409)
            if state["status"] not in {"running", "preparing", "blocked", "reconciliation_required"}:
                raise DomainError("not_resumable", "当前会话无需恢复", 409)
            error = state.get("last_error") or {}
            if state["status"] == "blocked" and error.get("code") not in {"provider_unavailable", "provider_http"}:
                raise DomainError("not_resumable", "该阻断不能自动恢复，请创建新分析", 409)
            if state.get("manifest") and not self._manifest_current(state, actor):
                raise DomainError("snapshot_changed", "恢复快照已变化", 409)
            if state["next_node"] == "end":
                state["next_node"] = error.get("node", "context_resolver")
            state["status"] = "running" if state["mode"] == "execute" else "preparing"
            state["last_error"] = None
            self._save_locked(db, state, "resumed")
            self._bind_action(db, actor, action_id, state)
            self.store.audit(db, actor.actor_id, "resume_requested", {}, session_id)
        self._run(state)
        return self._action_finish(actor, action_id, None)

    def resolve_call(self, session_id: str, call_id: str, actor: Actor, *,
                     note: str, result: dict | None = None) -> dict:
        self._owned(session_id, actor)
        if actor.role != "admin" or not 10 <= len(note) <= 1000:
            raise DomainError("reconcile_permission", "仅管理员可附明确核查说明对账", 403)
        schemas = {"intent": Intent, "plan": Plan, "hypothesizer": Hypotheses, "reflection": Reflection}
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM calls WHERE call_id=? AND session_id=?", (call_id, session_id)).fetchone()
            if row is None or row["status"] not in {"started", "unknown"}:
                raise DomainError("call_not_unknown", "不存在待对账推理", 409)
            active = db.execute("SELECT expires_at FROM leases WHERE session_id=?", (session_id,)).fetchone()
            if active and active["expires_at"] > time.time():
                raise DomainError("analysis_busy", "执行者尚未退出，不能对账", 409)
            if result is not None:
                value = schemas[row["kind"]].model_validate(result["value"])
                tokens = result.get("tokens")
                if type(tokens) is not int or tokens < 1:
                    raise DomainError("receipt_invalid", "对账结果必须含正整数真实token用量")
                ref = self.store.write_artifact("ModelResponse", {"value": value.model_dump(), "tokens": tokens})
                db.execute("UPDATE calls SET status='completed',response_ref=? WHERE call_id=?",
                           (canonical(ref.model_dump()), call_id))
                state = self._control_state(db, session_id)
                state["tokens"] += tokens
                self._save_locked(db, state, "receipt_reconciled")
                outcome = "receipt_recorded"
            else:
                # An explicit human declaration permits a fresh attempt, not exactly-once inference.
                db.execute("UPDATE calls SET status='retry_authorized',error_code='manual_no_result' WHERE call_id=?", (call_id,))
                outcome = "retry_authorized"
            self.store.audit(db, actor.actor_id, "call_reconciled", {"call_id": call_id, "note": note,
                                                                   "outcome": outcome}, session_id)
        return {"call_id": call_id, "status": outcome}

    def _wrapper(self, node):
        def run(state):
            started = time.monotonic()
            state = copy.deepcopy(state)
            with self.store.transaction() as db:
                self._assert_state(db, state)
            state["trace"] = [*state["trace"], node]
            try:
                actor = Actor.model_validate(state["actor"])
                self.store.current_actor(actor)
                if state["elapsed"] >= MAX_SECONDS:
                    raise DomainError("time_budget", "累计执行时间预算已耗尽")
                if state.get("manifest") and node not in {"report_renderer", "context_resolver"}:
                    if not self._manifest_current(state, actor):
                        raise DomainError("snapshot_changed", "取证快照已变化，停止分析", 409)
                getattr(self, "_" + node)(state)
                state["next_node"] = self._next(node, state)
            except DomainError as exc:
                state["last_error"] = {"code": exc.code, "message": exc.message, "node": node}
                state["status"] = "reconciliation_required" if exc.code in {"provider_unknown", "tool_unknown"} else "blocked"
                state["next_node"] = "end"
            except (ValidationError, ValueError, KeyError, TypeError):
                state["last_error"] = {"code": "invalid_state", "message": "结构或状态校验失败，未继续执行", "node": node}
                state["status"], state["next_node"] = "blocked", "end"
            finally:
                state["elapsed"] += time.monotonic() - started
                self._checkpoint(state, node)
            return state
        return run

    def _next(self, node, state):
        if state["status"] in {"needs_clarification", "out_of_scope", "awaiting_confirmation"}:
            return "end"
        if node == "report_renderer":
            return "end"
        if node == "loop_ctrl":
            return "hypothesizer" if state["sq_index"] < len(state["queue"]) else "report_renderer"
        if node == "hop_close":
            sq = state["queue"][state["sq_index"]]
            return "reflection" if state.get("last_error") and sq not in state["reflection_used"] else "loop_ctrl"
        if node == "reflection":
            return "verifier" if state["pending_hypotheses"] else "loop_ctrl"
        if node == "confirm":
            return "end"
        if node == "hypothesizer":
            return "verifier"
        if node == "verifier":
            return "hop_close"
        return NODES[NODES.index(node) + 1]

    def _entity(self, state, kind, payload, parent_id=None):
        with self.store.transaction() as db:
            self._assert_state(db, state)
            return self._entity_locked(db, state, kind, payload, parent_id)

    def _entity_locked(self, db, state, kind, payload, parent_id=None):
        identity = new_id(kind.lower())
        ref = self.store.write_artifact(kind, {"entity_id": identity, "session_id": state["session_id"],
            "round_id": state["round_id"], "manifest": state.get("manifest"), **payload})
        db.execute("INSERT INTO entities VALUES(?,?,?,?,?,?,?)",
                   (identity, state["session_id"], state["round_id"], kind, parent_id,
                    canonical(ref.model_dump()), now()))
        return identity

    def _load_entity(self, identity, state):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM entities WHERE entity_id=? AND session_id=? AND round_id=?",
                             (identity, state["session_id"], state["round_id"])).fetchone()
        if row is None:
            raise DomainError("evidence_scope", "证据身份不属于当前轮次", 409)
        return self.store.read_artifact(json.loads(row["payload_ref"]))

    def _call(self, state, stage, context, schema):
        key = digest({"round": state["round_id"], "stage": stage, "context": context})
        call_id = "call-" + key[:24]
        request_ref = self.store.write_artifact("ModelRequest", {"stage": stage, "context": context})
        with self.store.transaction() as db:
            self._assert_state(db, state)
            row = db.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row:
                if row["status"] == "completed":
                    if state["tokens"] > MAX_TOKENS:
                        raise DomainError("model_budget", "累计模型用量超过预算，不再使用输出")
                    result = self.store.read_artifact(json.loads(row["response_ref"]))
                    return schema.model_validate(result["value"])
                if row["status"] == "retry_authorized" or (row["status"] == "failed" and
                        row["error_code"] in {"provider_unavailable", "provider_http"}):
                    archived_id = new_id("call-history")
                    db.execute("UPDATE calls SET call_id=? WHERE call_id=?", (archived_id, call_id))
                else:
                    raise DomainError("provider_unknown", "发现未完成推理记录，必须先对账", 409)
            if state["calls"] >= MAX_CALLS or state["tokens"] >= MAX_TOKENS:
                raise DomainError("model_budget", "累计模型预算已耗尽")
            db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (call_id, state["session_id"], state["round_id"], stage, key, "started",
                        canonical(request_ref.model_dump()), None, None, now()))
            state["calls"] += 1
            self._save_locked(db, state, "call_reserved")
        try:
            reply = self.provider.complete(stage, context, schema)
        except DomainError as exc:
            with self.store.transaction() as db:
                self._assert_state(db, state)
                db.execute("UPDATE calls SET status=?,error_code=? WHERE call_id=?",
                           ("unknown" if exc.code == "provider_unknown" else "failed", exc.code, call_id))
            raise
        state["tokens"] += reply.tokens
        ref = self.store.write_artifact("ModelResponse", {"value": reply.value.model_dump(mode="json"),
                                                         "tokens": reply.tokens})
        with self.store.transaction() as db:
            db.execute("UPDATE calls SET status='completed',response_ref=? WHERE call_id=?",
                       (canonical(ref.model_dump()), call_id))
            self._save_locked(db, state, "call_completed")
        if state["tokens"] > MAX_TOKENS:
            raise DomainError("model_budget", "模型返回后累计用量超过预算，不再使用输出")
        return reply.value

    def _context_resolver(self, state):
        if not state["request"].get("scope"):
            state["status"] = "needs_clarification"
            state["last_error"] = {"code": "scope_required", "message": "请明确工厂、产品、开始/结束日期和指标，提交新的请求ID"}

    def _intent(self, state):
        answer = self._call(state, "intent", {"question": state["request"]["question"]}, Intent)
        if answer.kind != "quality_analysis":
            state["status"] = "needs_clarification" if answer.kind == "clarify" else "out_of_scope"
            state["last_error"] = {"code": answer.kind, "message": answer.explanation}

    def _tool(self, state, request, scope, actor, *, kind, slot, payload, parent_id=None):
        key = digest({"round": state["round_id"], "kind": kind, "slot": slot,
                      "request": request.model_dump(mode="json"), "scope": scope.model_dump(mode="json")})
        call_id = "tool-" + key[:24]
        request_ref = self.store.write_artifact("ToolRequest", request.model_dump(mode="json"))
        with self.store.transaction() as db:
            self._assert_state(db, state)
            row = db.execute("SELECT * FROM tool_calls WHERE call_id=?", (call_id,)).fetchone()
            if row:
                if row["status"] == "started":
                    raise DomainError("tool_unknown", "工具执行结果未知，禁止自动重试", 409)
                receipt = self.store.read_artifact(json.loads(row["response_ref"]))
                if row["status"] == "failed":
                    raise DomainError(**receipt["error"])
                return ToolResult.model_validate(receipt["result"]), row["entity_id"]
            if state["tools"] >= MAX_TOOLS:
                raise DomainError("tool_budget", "累计工具调用预算耗尽")
            db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?,?)",
                       (call_id, state["session_id"], state["round_id"], kind, "started",
                        canonical(request_ref.model_dump()), None, None, now()))
            state["tools"] += 1
            self._save_locked(db, state, "tool_reserved")
        error, result = None, None
        try:
            result = self.verifier.execute(request, scope, actor)
            if not self._manifest_current(state, actor):
                raise DomainError("snapshot_changed", "工具执行期间快照变化，结果不作为证据", 409)
        except DomainError as exc:
            error = {"code": exc.code, "message": exc.message, "status": exc.status}
        except Exception as exc:
            # No durable receipt: spend remains reserved, and resume must not repeat the tool.
            raise DomainError("tool_unknown", "工具未返回可持久化收据，禁止自动重试", 409) from exc
        receipt = {"error": error} if error else {"result": result.model_dump(mode="json")}
        ref = self.store.write_artifact("ToolReceipt", receipt)
        with self.store.transaction() as db:
            self._assert_state(db, state)
            eid = None
            if error is None:
                content = ({**payload, "summary": result.summary, "payload": result.payload}
                           if kind == "cohort" else {**payload, "result": result.model_dump(mode="json")})
                eid = self._entity_locked(db, state, "Observation" if kind == "cohort" else "Evidence",
                                          content, parent_id)
            db.execute("UPDATE tool_calls SET status=?,response_ref=?,entity_id=? WHERE call_id=?",
                       ("failed" if error else "completed", canonical(ref.model_dump()), eid, call_id))
            # Receipt, evidence identity and reserved budget commit together; node replay reuses them.
            self._save_locked(db, state, "tool_completed")
        if error:
            raise DomainError(**error)
        return result, eid

    def _cohort(self, state):
        from .schemas import ToolRequest
        scope = Scope.model_validate(state["request"]["scope"])
        actor = Actor.model_validate(state["actor"])
        if not state.get("manifest"):
            state["manifest"] = self._manifest(state, actor)
        cohort_key = "cohort-" + digest({"round": state["round_id"], "scope": state["request"]["scope"]})[:24]
        self._tool(state, ToolRequest(tool="metadata"), scope, actor, kind="cohort", slot=cohort_key,
                   payload={"observation_key": cohort_key})

    def _cache_guard(self, state):
        # Only immutable prior content can be reused; never reuse round-specific Evidence.
        state["cache_key"] = digest({"question": state["request"]["question"],
            "scope": state["request"]["scope"], "manifest": {
                k: v for k, v in state["manifest"].items() if k != "manifest_id"}})
        self._entity(state, "GuardAssessment", {"gate": "cache", "key": state["cache_key"],
                                               "decision": "exact_prior_only"})

    def _prior_check(self, state):
        actor = Actor.model_validate(state["actor"])
        with self.store.connect() as db:
            cached = db.execute("SELECT artifact_ref FROM caches WHERE cache_key=?",
                                (state["cache_key"],)).fetchone()
        if cached:
            reference = json.loads(cached["artifact_ref"])
            prior = self.store.read_artifact(reference)
            for hit in prior["hits"]:
                self.documents.validate_hit(hit, actor)
            state["prior_ref"] = reference
        else:
            result = self.documents.search(state["request"]["question"],
                                           Scope.model_validate(state["request"]["scope"]), actor)
            state["prior_ref"] = self.store.write_artifact("Prior", result.model_dump()).model_dump()
            if not self._manifest_current(state, actor):
                raise DomainError("snapshot_changed", "检索期间索引或授权变化", 409)
            with self.store.transaction() as db:
                db.execute("INSERT OR REPLACE INTO caches VALUES(?,?,?,?)",
                           (state["cache_key"], canonical(state["manifest"]),
                            canonical(state["prior_ref"]), now()))

    def _plan(self, state):
        prior = self.store.read_artifact(state["prior_ref"])
        plan = self._call(state, "plan", {"request": state["request"], "prior": prior,
            "allowed_sources": ["sql", "metadata", "work_orders", "document"],
            "grouping_contract": "每个SQ明确唯一group_by：各产线比较=line_id，材料批次=material_lot，"
                "班次=shift，逐日=day，供应商=supplier_id。比较各组时必须保留分组维度，"
                "即使问题使用‘总体对比’；null仅用于不比较任何分组的单个汇总值，并禁止GROUP BY。"
                "后续维度是独立extended SQ，不在core假设或Reflection中顺带执行；标题必须匹配分组。",
            "requirement": "core回答主要问题，extended仅列卡片。SQL表inspections，字段line_id,material_lot,supplier_id,shift,day,defect,rework,cycle_time。"}, Plan)
        state["plan"] = plan.model_dump()
        state["plan_fingerprint"] = digest({"plan": state["plan"], "manifest": state["manifest"]})

    def _confirm(self, state):
        state["status"] = "awaiting_confirmation"

    def _hypothesizer(self, state):
        sq = self._current_sq(state)
        decisions = {d["sq_id"]: d for d in state["decisions"]}
        if any(dep not in decisions or decisions[dep]["outcome"] != "observed"
               for dep in sq["depends_on"]):
            raise DomainError("dependency_unresolved", "依赖SQ未取得足够证据，阻止依赖分析", 409)
        if sq["sq_id"] not in state["authorized"]:
            raise DomainError("sq_not_authorized", "未授权SQ不能执行", 403)
        dependencies = []
        for dep in sq["depends_on"]:
            decision = decisions[dep]
            revision = self._load_entity(decision["revision_id"], state)
            evidence = [self._load_entity(eid, state) for eid in revision["evidence_ids"]]
            dependencies.append({"sq_id": dep, "revision_id": decision["revision_id"],
                "evidence": [{"evidence_id": e["entity_id"], "result": e["result"]} for e in evidence]})
        draft = self._call(state, "hypothesizer", {"request": self._sq_request(state, sq), "sq": sq,
            "dependencies": dependencies,
            "prior": self.store.read_artifact(state["prior_ref"]) if state.get("prior_ref") else {},
            "sql_contract": self._sq_sql_contract(state, sq)}, Hypotheses)
        pending = []
        for hypothesis in draft.hypotheses:
            hid = self._entity(state, "Hypothesis", {"sq_id": sq["sq_id"], **hypothesis.model_dump()})
            pending.append({"hypothesis_id": hid, **hypothesis.model_dump()})
        state["hypotheses"].extend(pending)
        state["pending_hypotheses"] = pending
        state["last_error"] = None

    @staticmethod
    def _sq_request(state, sq):
        # The original question may request future dimensions that are not authorized for this hop.
        return {"question": sq["title"], "scope": state["request"]["scope"]}

    @staticmethod
    def _sq_sql_contract(state, sq):
        metric = state["request"]["scope"]["metric_id"]
        expression = {"defect_rate": "SUM(defect) AS defects", "rework_rate": "SUM(rework) AS reworks",
                      "cycle_time_mean": "AVG(cycle_time) AS cycle_time_mean"}[metric]
        group = sq.get("group_by")
        example = (f"SELECT {group},COUNT(*) AS n,{expression} FROM inspections GROUP BY {group}"
                   if group else f"SELECT COUNT(*) AS n,{expression} FROM inspections")
        grouping = (f"ToolRequest.group_by必须为{group}，SQL必须且只能GROUP BY {group}"
                    if group else "总体SQ：ToolRequest.group_by必须为null，SQL不得含GROUP BY")
        return (f"只回答当前SQ，不执行原始问题中的其他SQ或未来需求。只查inspections授权子集。当前指标{metric}。"
                f"{grouping}。示例：{example}。不得更换、增加分组维度或换指标；"
                "依赖证据仅供当前SQ参考，不授权重新分析依赖维度。历史报告不是当前证据。")

    @staticmethod
    def _guard_sq_grouping(sq, request, metric=None):
        group = sq.get("group_by")
        if request.tool != "sql":
            if request.group_by is not None and request.group_by != group:
                raise DomainError("sq_grouping", "工具分组超出当前SQ授权维度", 403)
            return
        if request.group_by != group:
            raise DomainError("sq_grouping", "SQL工具分组必须匹配当前SQ授权维度（null表示总体）", 403)
        try:
            statements = parse(request.sql, read="sqlite")
        except (ParseError, TokenError) as exc:
            raise DomainError("sq_grouping", "无法验证SQL分组结构", 403) from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise DomainError("sq_grouping", "SQ只接受单一SELECT的明确分组结构", 403)
        tree = statements[0]
        if len(list(tree.find_all(exp.Select))) != 1:
            raise DomainError("sq_grouping", "嵌套查询不能绕过当前SQ分组范围", 403)
        groups = list(tree.find_all(exp.Group))
        if group is None:
            valid = not groups
        else:
            clause = tree.args.get("group")
            valid = (len(groups) == 1 and clause is not None and len(clause.expressions) == 1
                     and isinstance(clause.expressions[0], exp.Column)
                     and clause.expressions[0].name.lower() == group
                     and not any(value for key, value in clause.args.items() if key != "expressions"))
            # A SELECT alias must not relabel another dimension as the authorized grouping column.
            valid = valid and not any(alias.alias.lower() == group
                and (not isinstance(alias.this, exp.Column) or alias.this.name.lower() != group)
                for alias in tree.find_all(exp.Alias))
        if not valid:
            raise DomainError("sq_grouping", "SQL GROUP BY结构与当前SQ授权分组不一致", 403)
        tables = list(tree.find_all(exp.Table))
        if len(tables) != 1 or tables[0].name.lower() != "inspections":
            raise DomainError("sq_grouping", "质量指标SQ只能查询inspections，其他记录须用对应工具", 403)
        metric_columns = {"defect_rate": "defect", "rework_rate": "rework",
                          "cycle_time_mean": "cycle_time"}
        allowed_columns = ({metric_columns[metric]} if metric else set(metric_columns.values()))
        if group:
            allowed_columns.add(group)
        aliases = {projection.alias.lower() for projection in tree.expressions if projection.alias}
        aggregate_seen = False
        for projection in tree.expressions:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            aggregate_seen |= any(expression.find_all(exp.AggFunc))
            for column in expression.find_all(exp.Column):
                if column.name.lower() not in allowed_columns:
                    raise DomainError("sq_grouping", "SQL投影不能附带未授权维度或其他指标", 403)
                if column.name.lower() != group and column.find_ancestor(exp.AggFunc) is None:
                    raise DomainError("sq_grouping", "质量指标必须聚合，不能用原始记录冒充汇总", 403)
            for star in expression.find_all(exp.Star):
                if not isinstance(star.parent, exp.Count):
                    raise DomainError("sq_grouping", "质量SQ不允许原始通配列", 403)
        if not aggregate_seen:
            raise DomainError("sq_grouping", "质量SQ需要聚合观察而非无关记录", 403)
        for clause_name in ("having", "order"):
            clause = tree.args.get(clause_name)
            if clause is not None and any(column.name.lower() not in allowed_columns | aliases
                                          for column in clause.find_all(exp.Column)):
                raise DomainError("sq_grouping", "排序或聚合筛选不能引入未授权维度", 403)

    def _current_sq(self, state):
        key = state["queue"][state["sq_index"]]
        return next(sq for sq in state["plan"]["subquestions"] if sq["sq_id"] == key)

    def _verifier(self, state):
        from .schemas import ToolRequest
        actor, scope = Actor.model_validate(state["actor"]), Scope.model_validate(state["request"]["scope"])
        sq = self._current_sq(state)
        state["hop_evidence_ids"] = []
        for hypothesis in state["pending_hypotheses"]:
            for tool_index, raw in enumerate(hypothesis["tools"]):
                request = ToolRequest.model_validate(raw)
                try:
                    if request.tool not in sq["sources"]:
                        raise DomainError("source_not_authorized", "工具不在当前SQ授权源中", 403)
                    self._guard_sq_grouping(sq, request, scope.metric_id)
                    if not self._manifest_current(state, actor):
                        raise DomainError("snapshot_changed", "工具调用前快照变化", 409)
                    result, eid = self._tool(state, request, scope, actor, kind="evidence",
                        slot=[hypothesis["hypothesis_id"], tool_index], parent_id=hypothesis["hypothesis_id"],
                        payload={"sq_id": sq["sq_id"], "hypothesis_id": hypothesis["hypothesis_id"], "request": raw})
                except DomainError as exc:
                    gid = self._entity(state, "GuardAssessment", {"sq_id": sq["sq_id"],
                        "hypothesis_id": hypothesis["hypothesis_id"], "decision": "rejected",
                        "code": exc.code, "message": exc.message})
                    state["guard_ids"].append(gid)
                    state["last_error"] = {"code": exc.code, "message": exc.message}
                    if exc.code in {"snapshot_changed", "tool_budget", "tool_unknown", "stale_worker",
                                    "acl_changed", "unauthorized"}:
                        raise
                    continue
                if eid not in state["evidence_ids"]:
                    state["evidence_ids"].append(eid)
                state["hop_evidence_ids"].append(eid)
                if result.status != "ok":
                    state["last_error"] = {"code": result.status, "message": result.summary}
        state["pending_hypotheses"] = []

    def _hop_close(self, state):
        subquestion = self._current_sq(state)
        sq = subquestion["sq_id"]
        evidence = [self._load_entity(eid, state) for eid in state["hop_evidence_ids"]]
        current = [e for e in evidence if e["sq_id"] == sq]
        if "sql" in subquestion["sources"] and not any(
                e["request"]["tool"] == "sql" and e["result"]["status"] == "ok" for e in current):
            state["last_error"] = state.get("last_error") or {
                "code": "metric_evidence_missing", "message": "SQL指标SQ尚未取得有效聚合取证，其他来源不能替代。"}
        previous = next((d for d in state["decisions"] if d["sq_id"] == sq), None)
        previous_revision = self._load_entity(previous["revision_id"], state) if previous else None
        cid = previous_revision["claim_id"] if previous_revision else self._entity(state, "Claim", {
            "sq_id": sq, "level": "descriptive_only",
            "statement": "以下仅为授权样本的观测与统计结果，不证明假设或现实因果。"})
        rid = self._entity(state, "Revision", {"claim_id": cid,
            "status": "supported_observation" if current and not state.get("last_error") else "unresolved",
            "evidence_ids": [e["entity_id"] for e in current],
            "supersedes": previous["revision_id"] if previous else None}, cid)
        if not previous:
            state["claim_ids"].append(cid)
        state["revision_ids"].append(rid)
        decision = {"sq_id": sq, "revision_id": rid,
                    "outcome": "observed" if current and not state.get("last_error") else "unresolved"}
        did = self._entity(state, "Decision", decision, rid)
        state["decisions"] = [d for d in state["decisions"] if d["sq_id"] != sq] + [{"decision_id": did, **decision}]
        self._entity(state, "Hop", {"sq_id": sq, "decision_id": did, "revision_id": rid})

    def _reflection(self, state):
        sq = self._current_sq(state)
        contexts = state.setdefault("reflection_contexts", {})
        if sq["sq_id"] not in contexts:
            # Older checkpoints lack reflection_contexts; the reserved request is authoritative.
            with self.store.connect() as db:
                rows = db.execute("SELECT request_ref FROM calls WHERE session_id=? AND round_id=? "
                                  "AND kind='reflection' ORDER BY created_at",
                                  (state["session_id"], state["round_id"])).fetchall()
            for row in rows:
                saved = self.store.read_artifact(json.loads(row["request_ref"]))["context"]
                if saved.get("sq", {}).get("sq_id") == sq["sq_id"]:
                    contexts[sq["sq_id"]] = saved
                    break
        context = contexts.setdefault(sq["sq_id"], {"sq": sq, "request": self._sq_request(state, sq),
            "sql_contract": self._sq_sql_contract(state, sq),
            "failure": copy.deepcopy(state.get("last_error")),
            "allowed_actions": ["repair", "refine", "supplement", "no_action"]})
        if sq["sq_id"] not in state["reflection_used"]:
            state["reflection_used"].append(sq["sq_id"])
        # _call persists this context with the reservation, before contacting the model.
        result = self._call(state, "reflection", context, Reflection)
        state["last_error"] = copy.deepcopy(context["failure"])
        self._entity(state, "GuardAssessment", {"gate": "reflection", "sq_id": sq["sq_id"],
            "action": result.action, "budget_remaining": MAX_TOOLS - state["tools"]})
        state["pending_hypotheses"] = []
        if result.replacement:
            if any(t.tool not in sq["sources"] for t in result.replacement.tools):
                state["last_error"] = {"code": "reflection_scope", "message": "Reflection越出当前授权源，未执行"}
                return
            hid = self._entity(state, "Hypothesis", {"sq_id": sq["sq_id"], "reflection": result.action,
                                                     **result.replacement.model_dump()})
            state["pending_hypotheses"] = [{"hypothesis_id": hid, **result.replacement.model_dump()}]
            state["last_error"] = None

    def _loop_ctrl(self, state):
        state["sq_index"] += 1

    @staticmethod
    def _report_payload(result):
        if result["source"] != "sql":
            return copy.deepcopy(result["payload"])
        payload = result["payload"]
        return {"statistics": copy.deepcopy(payload.get("statistics", {})),
                "provenance": copy.deepcopy(payload.get("provenance", {})),
                "display_note": "仅展示程序独立统计；模型SQL投影仅保存在原始Evidence审计制品中。"}

    def _report_renderer(self, state):
        actor = Actor.model_validate(state["actor"])
        if not self._manifest_current(state, actor):
            raise DomainError("snapshot_changed", "报告生成前快照已失效", 409)
        sections = []
        for decision in state["decisions"]:
            revision = self._load_entity(decision["revision_id"], state)
            evidence = [self._load_entity(eid, state) for eid in revision["evidence_ids"]]
            for item in evidence:
                for citation in item["result"]["citations"]:
                    self.documents.validate_hit(citation, actor)
            sections.append({"sq_id": decision["sq_id"], "revision_id": decision["revision_id"],
                "outcome": decision["outcome"], "evidence": [{"evidence_id": e["entity_id"],
                    "summary": e["result"]["summary"], "source": e["result"]["source"],
                    "payload": self._report_payload(e["result"]), "citations": e["result"]["citations"]} for e in evidence]})
        unexecuted = [sq for sq in state["plan"]["subquestions"] if sq["sq_id"] not in state["authorized"]]
        report = {"session_id": state["session_id"], "round_id": state["round_id"],
            "manifest": state["manifest"], "scope": state["request"]["scope"], "sections": sections,
            "unexecuted": unexecuted, "limitations": ["原创合成数据，不是企业生产效果。",
                "描述与关联不证明因果；建议需人工审核，不执行生产措施。",
                "报告数字由程序引用当前轮次有效证据，不由生成模型自由改写。"],
            "provider": self.provider.fingerprint}
        state["report_ref"] = self.store.write_artifact("Report", report).model_dump()
        state["status"] = "limited" if any(d["outcome"] != "observed" for d in state["decisions"]) else "completed"

    def calls(self, session_id: str, actor: Actor) -> list[dict]:
        self._owned(session_id, actor)
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT call_id,round_id,kind,status,error_code,created_at FROM calls WHERE session_id=? ORDER BY created_at",
                (session_id,))]

    def audit(self, session_id: str, actor: Actor) -> list[dict]:
        self._owned(session_id, actor)
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit WHERE session_id=? ORDER BY id", (session_id,))]
