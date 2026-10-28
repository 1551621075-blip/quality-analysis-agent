"""Deterministic recovery windows; these tests do not claim process-kill exactly-once."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_graph import app as graph_app
from test_graph import confirmation, request

from quality_analysis.provider import ModelReply, SyntheticProvider
from quality_analysis.schemas import DomainError, FollowupRequest, Reflection
from quality_analysis.service import MAX_CALLS, MAX_TOKENS, MAX_TOOLS, AnalysisService
from quality_analysis.storage import canonical


@pytest.fixture
def app(tmp_path):
    return graph_app.__wrapped__(tmp_path)


def restart(service):
    return AnalysisService(service.store, service.provider, service.documents, service.verifier)


def interrupt_node_checkpoint(service, target):
    checkpoint = service._checkpoint

    def interrupted(state, node):
        if node == target:
            raise RuntimeError("interruption before node checkpoint")
        checkpoint(state, node)

    service._checkpoint = interrupted


def test_delayed_worker_cannot_execute_or_replace_newer_round(app):
    service, actor, _, verifier = app
    prepared = service.analyze(request(), actor)
    entered, release = threading.Event(), threading.Event()
    original_run = service._run
    pause = True

    def delayed(state):
        nonlocal pause
        if pause:
            pause = False
            entered.set()
            assert release.wait(10)
        return original_run(state)

    service._run = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.confirm, prepared["session_id"], confirmation(prepared), actor)
        assert entered.wait(10)
        try:
            service.resume(prepared["session_id"], "recover-delayed", actor)
            latest = service.followup(prepared["session_id"], FollowupRequest(
                request_id="newer", question="Check authorized orders", selected_sq_ids=["orders"]), actor)
            calls = len(verifier.requests)
        finally:
            release.set()
        with pytest.raises(DomainError, match="旧执行者") as error:
            pending.result(timeout=10)
        assert error.value.code == "stale_worker"
    assert service.get(prepared["session_id"], actor) == latest
    assert len(verifier.requests) == calls
    # The delayed confirmation's receipt is tied to its original round, not latest.
    replay = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert replay["round_id"] == prepared["round_id"] != latest["round_id"]


def test_expired_worker_cannot_commit_or_overwrite_reconciled_call(app):
    service, _, _, _ = app
    actor = service.store.authenticate(service.store.provision_user("admin", "admin", ["plant-a"]))
    entered, release = threading.Event(), threading.Event()

    class Waiting(SyntheticProvider):
        def complete(self, stage, context, schema):
            if stage == "intent":
                entered.set()
                assert release.wait(10)
            return super().complete(stage, context, schema)

    service.provider = Waiting()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.analyze, request(), actor)
        assert entered.wait(10)
        try:
            session = service.list_sessions(actor)[0]
            with service.store.transaction() as db:
                db.execute("UPDATE leases SET expires_at=0 WHERE session_id=?", (session["session_id"],))
            call = service.calls(session["session_id"], actor)[0]
            service.resolve_call(session["session_id"], call["call_id"], actor,
                note="Local fixture receipt independently verified", result={
                    "value": {"kind": "quality_analysis", "explanation": "reconciled receipt"}, "tokens": 17})
            recovered = service.resume(session["session_id"], "new-worker", actor)
        finally:
            release.set()
        with pytest.raises(DomainError) as error:
            pending.result(timeout=10)
        assert error.value.code == "stale_worker"
    assert service.get(session["session_id"], actor) == recovered
    with service.store.connect() as db:
        row = db.execute("SELECT response_ref FROM calls WHERE call_id=?", (call["call_id"],)).fetchone()
    assert service.store.read_artifact(json.loads(row[0]))["tokens"] == 17


def test_tool_receipt_replay_preserves_evidence_and_spend_after_repeated_interruptions(app):
    service, actor, _, verifier = app
    prepared = service.analyze(request(), actor)
    interrupt_node_checkpoint(service, "verifier")
    with pytest.raises(RuntimeError):
        service.confirm(prepared["session_id"], confirmation(prepared), actor)
    for attempt in range(MAX_TOOLS):
        with pytest.raises(RuntimeError):
            service.resume(prepared["session_id"], f"again-{attempt}", actor)
    with service.store.connect() as db:
        evidence = db.execute("SELECT entity_id FROM entities WHERE kind='Evidence'").fetchall()
        receipts = db.execute("SELECT status FROM tool_calls").fetchall()
    assert len(evidence) == 1
    assert all(row[0] == "completed" for row in receipts)
    recovered = restart(service).resume(prepared["session_id"], "finish", actor)
    assert recovered["status"] == "completed"
    assert len(verifier.requests) == recovered["tools"] == 2
    visible = recovered["report"]["sections"][0]["evidence"]
    assert [item["evidence_id"] for item in visible] == [evidence[0][0]]


def test_tool_receipt_evidence_and_checkpoint_rollback_atomically(app):
    service, actor, _, verifier = app
    prepared = service.analyze(request(), actor)
    save = service._save_locked

    def interrupted_commit(db, state, node):
        if node == "tool_completed":
            raise RuntimeError("interruption after evidence insert, before receipt transaction commits")
        save(db, state, node)

    service._save_locked = interrupted_commit
    with pytest.raises(RuntimeError):
        service.confirm(prepared["session_id"], confirmation(prepared), actor)
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entities WHERE kind='Evidence'").fetchone()[0] == 0
        row = db.execute("SELECT * FROM tool_calls WHERE kind='evidence'").fetchone()
        assert row["status"] == "started" and row["response_ref"] is None and row["entity_id"] is None
    stopped = service.get(prepared["session_id"], actor)
    assert stopped["tools"] == len(verifier.requests) == 2
    with pytest.raises(DomainError) as error:
        restart(service).resume(prepared["session_id"], "unknown-after-rollback", actor)
    assert error.value.code == "tool_unknown"
    assert len(verifier.requests) == 2


def test_cohort_receipt_replay_does_not_repeat_metadata(app):
    service, actor, _, verifier = app
    interrupt_node_checkpoint(service, "cohort")
    with pytest.raises(RuntimeError):
        service.analyze(request(), actor)
    session = service.list_sessions(actor)[0]
    recovered = restart(service).resume(session["session_id"], "cohort-recovery", actor)
    assert recovered["status"] == "awaiting_confirmation"
    assert recovered["tools"] == len(verifier.requests) == 1
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entities WHERE kind='Observation'").fetchone()[0] == 1


@pytest.mark.parametrize("stage", ["cohort", "verifier"])
def test_interrupted_tool_without_receipt_keeps_budget_and_blocks_retry(app, stage):
    service, actor, _, verifier = app
    prepared = service.analyze(request(), actor) if stage == "verifier" else None
    execute = verifier.execute

    def uncertain(*args):
        execute(*args)
        raise RuntimeError("tool returned externally, no durable local receipt")

    verifier.execute = uncertain
    stopped = (service.confirm(prepared["session_id"], confirmation(prepared), actor)
               if prepared else service.analyze(request(), actor))
    assert stopped["status"] == "reconciliation_required"
    assert stopped["last_error"]["code"] == "tool_unknown"
    assert stopped["tools"] == len(verifier.requests)
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tool_calls WHERE status='started'").fetchone()[0] == 1
    attempts = len(verifier.requests)
    for i in range(2):
        with pytest.raises(DomainError) as error:
            restart(service).resume(stopped["session_id"], f"unsafe-{i}", actor)
        assert error.value.code == "tool_unknown"
    assert len(verifier.requests) == attempts


def test_tool_budget_reserved_before_dispatch_and_receipt_reusable_at_limit(app):
    service, actor, _, verifier = app
    prepared = service.analyze(request(), actor)
    with service.store.transaction() as db:
        row = db.execute("SELECT state_json FROM sessions WHERE session_id=?", (prepared["session_id"],)).fetchone()
        state = json.loads(row[0])
        state["tools"] = MAX_TOOLS - 1
        db.execute("UPDATE sessions SET state_json=? WHERE session_id=?", (canonical(state), prepared["session_id"]))
    execute = verifier.execute

    def inspect_reservation(*args):
        with service.store.connect() as db:
            state = json.loads(db.execute("SELECT state_json FROM sessions WHERE session_id=?",
                                          (prepared["session_id"],)).fetchone()[0])
            assert state["tools"] == MAX_TOOLS
            assert db.execute("SELECT 1 FROM tool_calls WHERE status='started'").fetchone()
        return execute(*args)

    verifier.execute = inspect_reservation
    interrupt_node_checkpoint(service, "verifier")
    with pytest.raises(RuntimeError):
        service.confirm(prepared["session_id"], confirmation(prepared), actor)
    recovered_service = restart(service)
    recovered = recovered_service.resume(prepared["session_id"], "budget-recovery", actor)
    assert recovered["status"] == "completed" and recovered["tools"] == MAX_TOOLS
    before = len(verifier.requests)
    blocked = recovered_service.followup(prepared["session_id"], FollowupRequest(
        request_id="no-reset", question="Try another round", selected_sq_ids=["core-rate"]), actor)
    assert blocked["last_error"]["code"] == "tool_budget"
    assert len(verifier.requests) == before


@pytest.mark.parametrize("legacy_context", [False, True])
def test_reconciled_reflection_reuses_original_context_and_receipt(app, legacy_context):
    service, _, _, verifier = app
    actor = service.store.authenticate(service.store.provision_user("admin", "admin", ["plant-a"]))

    class UnknownReflection(SyntheticProvider):
        def __init__(self):
            self.contexts = []

        def complete(self, stage, context, schema):
            if stage == "reflection":
                self.contexts.append(context)
                raise DomainError("provider_unknown", "synthetic unknown", 503)
            return super().complete(stage, context, schema)

    provider = UnknownReflection()
    service.provider = provider
    verifier.reject_sql = True
    prepared = service.analyze(request(), actor)
    stopped = service.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert stopped["status"] == "reconciliation_required"
    call = next(c for c in service.calls(prepared["session_id"], actor) if c["kind"] == "reflection")
    service.resolve_call(prepared["session_id"], call["call_id"], actor,
        note="Local verified repair receipt with original request context", result={"value": {
            "action": "repair", "reason": "Use the authorized metric query",
            "replacement": {"hypothesis_key": "receipt-repair", "statement": "Check current metric",
                            "tools": [{"tool": "sql", "group_by": "line_id", "sql":
                                "SELECT line_id,COUNT(*) AS n,SUM(defect) AS defects FROM inspections GROUP BY line_id"}]}}, "tokens": 51})
    if legacy_context:
        with service.store.transaction() as db:
            row = db.execute("SELECT state_json FROM sessions WHERE session_id=?", (prepared["session_id"],)).fetchone()
            state = json.loads(row[0])
            state.pop("reflection_contexts")
            db.execute("UPDATE sessions SET state_json=? WHERE session_id=?",
                       (canonical(state), prepared["session_id"]))
    verifier.reject_sql = False
    recovered = restart(service).resume(prepared["session_id"], "receipt-recovery", actor)
    assert recovered["status"] == "completed"
    assert len(provider.contexts) == 1
    assert provider.contexts[0]["failure"]["code"] == "sql_denied"
    assert len([c for c in service.calls(prepared["session_id"], actor) if c["kind"] == "reflection"]) == 1
    assert recovered["report"]["sections"][0]["evidence"]
    _, state = service._owned(prepared["session_id"], actor)
    assert state["reflection_used"] == ["core-rate"]
    with service.store.connect() as db:
        revisions = db.execute("SELECT entity_id,payload_ref FROM entities WHERE kind='Revision' ORDER BY created_at").fetchall()
    first, final = [service.store.read_artifact(json.loads(row["payload_ref"])) for row in revisions]
    assert final["claim_id"] == first["claim_id"]
    assert final["supersedes"] == revisions[0]["entity_id"]


def test_completed_reflection_checkpoint_reuses_context_without_new_call(app):
    service, actor, _, verifier = app

    class Repair(SyntheticProvider):
        def __init__(self):
            self.reflections = 0

        def complete(self, stage, context, schema):
            if stage == "reflection":
                self.reflections += 1
                return ModelReply(Reflection.model_validate({"action": "repair", "reason": "metric SQL repair",
                    "replacement": {"hypothesis_key": "repair", "statement": "Check metric",
                                    "tools": [{"tool": "sql", "group_by": "line_id", "sql":
                                        "SELECT line_id,COUNT(*) AS n,SUM(defect) AS defects FROM inspections GROUP BY line_id"}]}}), 20)
            return super().complete(stage, context, schema)

    provider = Repair()
    service.provider = provider
    verifier.reject_sql = True
    prepared = service.analyze(request(), actor)
    interrupt_node_checkpoint(service, "reflection")
    with pytest.raises(RuntimeError):
        service.confirm(prepared["session_id"], confirmation(prepared), actor)
    verifier.reject_sql = False
    recovered = restart(service).resume(prepared["session_id"], "reflection-checkpoint", actor)
    assert recovered["status"] == "completed"
    assert provider.reflections == 1


@pytest.mark.parametrize("operation", ["confirm", "followup", "resume"])
def test_action_outcome_recovered_from_matching_round_not_latest(app, operation):
    service, actor, _, verifier = app
    prepared = service.analyze(request(), actor)
    session_id = prepared["session_id"]
    action = confirmation(prepared)
    follow = FollowupRequest(request_id="first-follow", question="Check orders", selected_sq_ids=["orders"])
    if operation == "followup":
        service.confirm(session_id, action, actor)
    elif operation == "resume":
        interrupt_node_checkpoint(service, "verifier")
        with pytest.raises(RuntimeError):
            service.confirm(session_id, action, actor)
        service = restart(service)

    def invoke(target):
        if operation == "confirm":
            return target.confirm(session_id, action, actor)
        if operation == "followup":
            return target.followup(session_id, follow, actor)
        return target.resume(session_id, "first-resume", actor)

    def interrupted_finish(*args):
        raise RuntimeError("interrupted before action response commit")

    service._action_finish = interrupted_finish
    with pytest.raises(RuntimeError):
        invoke(service)
    original = service.get(session_id, actor)
    assert original["status"] == "completed"
    service = restart(service)
    latest = service.followup(session_id, FollowupRequest(request_id="unrelated",
        question="A later authorized round", selected_sq_ids=["core-rate"]), actor)
    attempts = len(verifier.requests)
    assert latest["round_id"] != original["round_id"]
    assert invoke(service) == original
    assert invoke(service) == original
    assert len(verifier.requests) == attempts
    assert service.get(session_id, actor) == latest


def test_recovered_action_outcome_still_checks_current_authorization(app):
    service, actor, _, _ = app
    prepared = service.analyze(request(), actor)

    def interrupted_finish(*args):
        raise RuntimeError("interrupted receipt save")

    service._action_finish = interrupted_finish
    with pytest.raises(RuntimeError):
        service.confirm(prepared["session_id"], confirmation(prepared), actor)
    with service.store.transaction() as db:
        db.execute("UPDATE users SET acl_version=acl_version+1 WHERE actor_id=?", (actor.actor_id,))
    actor = actor.model_copy(update={"acl_version": actor.acl_version + 1})
    recovered = restart(service)
    with pytest.raises(DomainError) as error:
        recovered.confirm(prepared["session_id"], confirmation(prepared), actor)
    assert error.value.code == "snapshot_changed"
    hidden = recovered.get(prepared["session_id"], actor)
    assert hidden["plan"] is None and hidden["report"]["sections"] == []


@pytest.mark.parametrize("counter,limit", [("calls", MAX_CALLS), ("tokens", MAX_TOKENS)])
def test_completed_model_receipt_can_replay_at_budget_boundary(app, counter, limit):
    service, actor, _, _ = app
    result = service.analyze(request(), actor)
    with service.store.transaction() as db:
        row = db.execute("SELECT state_json FROM checkpoints WHERE session_id=? AND node='call_completed' "
                         "ORDER BY version DESC LIMIT 1", (result["session_id"],)).fetchone()
        state = json.loads(row[0])
        state[counter] = limit
        db.execute("UPDATE sessions SET state_json=?,status='preparing' WHERE session_id=?",
                   (canonical(state), result["session_id"]))
    before = len(service.calls(result["session_id"], actor))
    recovered = restart(service).resume(result["session_id"], "budget-model-replay", actor)
    assert recovered["status"] == "awaiting_confirmation"
    assert recovered[counter] == limit
    assert len(service.calls(result["session_id"], actor)) == before
