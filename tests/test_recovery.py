import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_graph import app as graph_app
from test_graph import request

from quality_analysis.provider import SyntheticProvider
from quality_analysis.schemas import DomainError
from quality_analysis.service import AnalysisService
from quality_analysis.storage import canonical


@pytest.fixture
def app(tmp_path):
    return graph_app.__wrapped__(tmp_path)


class FailingProvider(SyntheticProvider):
    def __init__(self, code):
        self.code, self.attempts = code, 0

    def complete(self, stage, context, schema):
        self.attempts += 1
        if self.code:
            raise DomainError(self.code, "测试用模型故障", 503)
        return super().complete(stage, context, schema)


def test_unavailable_provider_can_resume_explicitly_without_resetting_budget(app):
    service, actor, *_ = app
    provider = FailingProvider("provider_unavailable")
    service.provider = provider
    result = service.analyze(request(), actor)
    assert result["status"] == "blocked" and provider.attempts == 1
    provider.code = None
    # Rebuild application using persisted checkpoint; no implicit restart/new state.
    resumed = AnalysisService(service.store, provider, service.documents, service.verifier)
    result = resumed.resume(result["session_id"], "resume-1", actor)
    assert result["status"] == "awaiting_confirmation", result.get("last_error")
    assert result["calls"] == 3
    attempts = provider.attempts
    assert resumed.resume(result["session_id"], "resume-1", actor) == result
    assert provider.attempts == attempts
    calls = resumed.calls(result["session_id"], actor)
    assert len(calls) == 3 and any(c["status"] == "failed" for c in calls)


def test_unknown_provider_requires_admin_reconciliation(app):
    service, actor, *_ = app
    provider = FailingProvider("provider_unknown")
    service.provider = provider
    result = service.analyze(request(), actor)
    assert result["status"] == "reconciliation_required"
    with pytest.raises(DomainError) as error:
        service.resume(result["session_id"], "cannot", actor)
    assert error.value.code == "provider_unknown"
    call = service.calls(result["session_id"], actor)[0]
    with pytest.raises(DomainError) as error:
        service.resolve_call(result["session_id"], call["call_id"], actor, note="人工检查确认没有可用结果")
    assert error.value.code == "reconcile_permission"
    # Test fixture role promotion is explicit server data, never a client supplied role.
    with service.store.transaction() as db:
        db.execute("UPDATE users SET role='admin',acl_version=2 WHERE actor_id=?", (actor.actor_id,))
    # Use fresh admin identity; this scope change must prevent old state resumption.
    admin = actor.model_copy(update={"role": "admin", "acl_version": 2})
    service.resolve_call(result["session_id"], call["call_id"], admin, note="人工核对本地服务未保存可用结果")
    assert service.calls(result["session_id"], admin)[0]["status"] == "retry_authorized"


def test_unknown_admin_receipt_drives_recovery_without_rerunning_call(app):
    service, _, *_ = app
    token = service.store.provision_user("admin", "admin", ["plant-a"])
    admin = service.store.authenticate(token)
    provider = FailingProvider("provider_unknown")
    service.provider = provider
    result = service.analyze(request(), admin)
    call = service.calls(result["session_id"], admin)[0]
    response = service.resolve_call(result["session_id"], call["call_id"], admin,
        note="根据本地推理服务收据核验结构化返回与用量",
        result={"value": {"kind": "quality_analysis", "explanation": "核验的合成测试收据"}, "tokens": 51})
    assert response["status"] == "receipt_recorded"
    provider.code = None
    resumed = service.resume(result["session_id"], "after-receipt", admin)
    assert resumed["status"] == "awaiting_confirmation", resumed.get("last_error")
    assert provider.attempts == 2  # failed intent + new plan; reconciled intent is not called again
    assert resumed["tokens"] == 179


def test_unknown_retry_permission_does_not_claim_exactly_once(app):
    service, _, *_ = app
    token = service.store.provision_user("admin", "admin", ["plant-a"])
    admin = service.store.authenticate(token)
    provider = FailingProvider("provider_unknown")
    service.provider = provider
    result = service.analyze(request(), admin)
    call = service.calls(result["session_id"], admin)[0]
    service.resolve_call(result["session_id"], call["call_id"], admin,
                         note="已检查服务日志无可用返回，明确允许再执行一次")
    provider.code = None
    resumed = service.resume(result["session_id"], "retry-authorized", admin)
    assert resumed["status"] == "awaiting_confirmation"
    assert provider.attempts == 3
    assert any(c["status"] == "retry_authorized" for c in service.calls(result["session_id"], admin))


def test_completed_model_call_recovery_uses_cached_result(app):
    service, actor, *_ = app
    result = service.analyze(request(), actor)
    with service.store.transaction() as db:
        rows = db.execute("SELECT state_json FROM checkpoints WHERE session_id=? AND node='call_completed' ORDER BY version",
                          (result["session_id"],)).fetchall()
        checkpoint = json.loads(rows[-1][0])
        checkpoint["status"] = "preparing"
        db.execute("UPDATE sessions SET state_json=?,status='preparing' WHERE session_id=?",
                   (canonical(checkpoint), result["session_id"]))
    count = len(service.calls(result["session_id"], actor))
    recovered = service.resume(result["session_id"], "restart", actor)
    assert recovered["status"] == "awaiting_confirmation"
    assert len(service.calls(result["session_id"], actor)) == count


def test_active_worker_prevents_resume_and_manual_reconciliation(app):
    service, actor, *_ = app
    entered, release = threading.Event(), threading.Event()
    class WaitingProvider(SyntheticProvider):
        def complete(self, stage, context, schema):
            if stage == "intent":
                entered.set()
                assert release.wait(10)
            return super().complete(stage, context, schema)
    service.provider = WaitingProvider()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.analyze, request(), actor)
        assert entered.wait(10)
        try:
            session = service.list_sessions(actor)[0]
            with pytest.raises(DomainError) as error:
                service.resume(session["session_id"], "racing", actor)
            assert error.value.code == "analysis_busy"
        finally:
            release.set()
        assert future.result()["status"] == "awaiting_confirmation"
