"""Actual SQLite/service tests with explicitly injected synthetic test provider, NOT LLM tests."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from quality_analysis.api import MAX_BODY, configured_provider, create_app
from quality_analysis.business import example_scope, seed_business
from quality_analysis.documents import original_reports
from quality_analysis.provider import LocalProvider, SyntheticProvider
from quality_analysis.schemas import DomainError


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path, SyntheticProvider())
    store = app.state.store
    tokens = {role: store.provision_user(role, role, ["PLANT-A"]) for role in ("operator", "analyst", "admin")}
    tokens["other"] = store.provision_user("other", "admin", ["PLANT-A"])
    seed_business(store)
    return app, TestClient(app), tokens


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def request(request_id="analysis-request"):
    return {"request_id": request_id, "question": "分析各产线缺陷率，先确认核心计划",
            "scope": example_scope().model_dump(mode="json")}


def test_real_plan_confirm_followup_owner_and_audit(setup):
    app, client, tokens = setup
    headers = auth(tokens["operator"])
    response = client.post("/api/sessions", json=request(), headers=headers)
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared["status"] == "awaiting_confirmation"
    assert prepared["tools"] == 1  # Scope metadata only, no hypothesis/SQ execution.
    assert "hypothesizer" not in prepared["trace"] and prepared["authorized"] == []
    assert prepared["provider"].startswith("synthetic-test")
    with app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entities WHERE kind IN ('Hypothesis','Evidence')").fetchone()[0] == 0
    sid = prepared["session_id"]
    assert client.post("/api/sessions", json=request(), headers=headers).json()["session_id"] == sid
    assert client.get(f"/api/sessions/{sid}", headers=auth(tokens["other"])).status_code == 404
    assert client.get("/api/sessions", headers=auth(tokens["other"])).json() == {"sessions": []}
    assert client.get(f"/api/sessions/{sid}/audit", headers=auth(tokens["other"])).status_code == 404
    bad = {"action_id": "bad-core", "selected_sq_ids": ["lot-strata"], "plan_fingerprint": prepared["plan_fingerprint"]}
    assert client.post(f"/api/sessions/{sid}/confirm", json=bad, headers=headers).status_code == 409
    confirm = {**bad, "action_id": "confirm-one", "selected_sq_ids": ["core-rate"]}
    confirmed = client.post(f"/api/sessions/{sid}/confirm", json=confirm, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    first = confirmed.json()
    assert first["status"] == "completed", first
    assert len(first["report"]["sections"]) == 1
    assert first["report"]["sections"][0]["evidence"]
    first_eid = first["report"]["sections"][0]["evidence"][0]["evidence_id"]
    assert {sq["sq_id"] for sq in first["report"]["unexecuted"]} == {"lot-strata", "orders"}
    repeated = client.post(f"/api/sessions/{sid}/confirm", json=confirm, headers=headers).json()
    assert repeated["round_id"] == first["round_id"]
    followed = client.post(f"/api/sessions/{sid}/followup", headers=headers, json={
        "request_id": "follow-one", "question": "按材料批次分层，包含核心依赖重新取证", "selected_sq_ids": ["lot-strata"]})
    assert followed.status_code == 200, followed.text
    latest = followed.json()
    assert latest["status"] == "completed", latest
    assert latest["round_id"] != first["round_id"]
    assert set(latest["authorized"]) == {"core-rate", "lot-strata"}
    assert all(e["evidence_id"] != first_eid for s in latest["report"]["sections"] for e in s["evidence"])
    audit = client.get(f"/api/sessions/{sid}/audit", headers=headers).json()["audit"]
    assert {row["action"] for row in audit} >= {"analysis_requested", "plan_confirmed", "followup_authorized"}
    with app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM rounds WHERE session_id=?", (sid,)).fetchone()[0] == 2
    assert tokens["operator"] not in json.dumps(audit)


def test_auth_server_roles_validation_and_clarification(setup):
    app, client, tokens = setup
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me?token=" + tokens["operator"]).status_code == 401
    assert client.get("/api/me", headers=auth("not-valid")).status_code == 401
    me = client.get("/api/me", headers=auth(tokens["operator"])).json()
    assert me["actor"]["role"] == "operator"
    assert me["provider"]["mode"] == "synthetic-test"
    assert me["example_scope"] == example_scope().model_dump(mode="json")
    hostile = {**request(), "role": "admin", "credentials": "SENSITIVE_DO_NOT_RETURN"}
    invalid = client.post("/api/sessions", json=hostile, headers=auth(tokens["operator"]))
    assert invalid.status_code == 422
    assert "SENSITIVE_DO_NOT_RETURN" not in invalid.text
    assert "input" not in invalid.text and "ctx" not in invalid.text
    forbidden = request()
    forbidden["scope"]["plant_id"] = "PLANT-B"
    assert client.post("/api/sessions", json=forbidden, headers=auth(tokens["admin"])).status_code == 403
    clarify = client.post("/api/sessions", json={"request_id": "clarify", "question": "比较质量问题"}, headers=auth(tokens["operator"]))
    assert clarify.status_code == 200
    assert clarify.json()["status"] == "needs_clarification"
    assert clarify.json()["last_error"]["code"] == "scope_required"
    assert clarify.json()["calls"] == 0
    with app.state.store.transaction() as db:
        db.execute("UPDATE users SET active=0 WHERE actor_id='operator'")
    assert client.get("/api/me", headers=auth(tokens["operator"])).status_code == 401


def test_unexpected_errors_never_echo_exception_or_token(setup, monkeypatch, caplog):
    app, client, tokens = setup
    def failure(*args):
        raise RuntimeError("SENSITIVE_EXCEPTION " + tokens["operator"])
    monkeypatch.setattr(app.state.service, "list_sessions", failure)
    response = client.get("/api/sessions", headers=auth(tokens["operator"]))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert tokens["operator"] not in response.text + caplog.text
    assert "SENSITIVE_EXCEPTION" not in response.text + caplog.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_reject_nonfinite_at_any_depth(setup, number):
    _, client, tokens = setup
    raw = '{"request_id":"finite","question":"问题","extra":{"nested":[' + number + ']}}'
    response = client.post("/api/sessions", content=raw, headers={**auth(tokens["operator"]), "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_streamed_body_limit_including_upload(setup):
    _, client, tokens = setup
    def stream():
        yield b'{"question":"'
        for _ in range(17):
            yield b"x" * 65536
        yield b'"}'
    for endpoint in ("/api/sessions", "/api/documents/upload"):
        response = client.post(endpoint, content=stream(), headers={**auth(tokens["admin"]), "Content-Type": "application/json", "Content-Length": "2"})
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "body_too_large"
    exact = b" " * (MAX_BODY - 2) + b"{}"
    assert client.post("/api/sessions", content=exact, headers={**auth(tokens["operator"]), "Content-Type": "application/json"}).status_code == 422
    assert client.post("/api/sessions", content=b"{}", headers=auth(tokens["operator"])).status_code == 415
    assert client.post("/api/sessions", content=b"[]", headers={**auth(tokens["operator"]), "Content-Type": "application/json"}).status_code == 400


def test_documents_upload_ingest_search_and_admin_acl(setup):
    app, client, tokens = setup
    report = original_reports("PLANT-A")[0].model_dump(mode="json")
    operator = auth(tokens["operator"])
    assert client.post("/api/documents/ingest", json=report, headers={**operator, "X-Role": "admin"}).status_code == 403
    # Both analyst and admin can ingest; ACL changes remain admin-only.
    ingested = client.post("/api/documents/ingest", json=report, headers=auth(tokens["analyst"]))
    assert ingested.status_code == 200, ingested.text
    upload = {k: v for k, v in report.items() if k != "text"}
    upload.update(document_id="UPLOAD-1", filename="synthetic-report.md", content_base64=base64.b64encode(report["text"].encode()).decode())
    response = client.post("/api/documents/upload", json=upload, headers=auth(tokens["analyst"]))
    assert response.status_code == 200, response.text
    search = {"query": "valve seals storage moisture ridges", "scope": example_scope().model_dump(mode="json")}
    found = client.post("/api/documents/search", json=search, headers=operator)
    assert found.status_code == 200
    assert found.json()["hits"] and found.json()["low_prior"] is True
    for hit in found.json()["hits"]:
        assert hit["text"] == report["text"][hit["quote_start"]:hit["quote_end"]]
    hit = found.json()["hits"][0]
    drill_path = f"/api/documents/{hit['document_id']}/chunks/{hit['chunk_id']}"
    assert client.get(drill_path, headers=operator).json()["text"] == hit["text"]
    assert client.get(drill_path, headers=auth(tokens["other"])).status_code == 409
    docid = report["document_id"]
    assert client.patch(f"/api/documents/{docid}/acl", json={"shared_with": ["admin"]}, headers=auth(tokens["analyst"])).status_code == 403
    assert client.delete(f"/api/documents/{docid}", headers=auth(tokens["analyst"])).status_code == 403
    for key in (docid, "UPLOAD-1"):
        assert client.patch(f"/api/documents/{key}/acl", json={"shared_with": ["admin"]}, headers=auth(tokens["admin"])).status_code == 200
    assert client.post("/api/documents/search", json=search, headers=operator).json()["hits"] == []
    assert client.get(drill_path, headers=operator).status_code in {404, 409}
    assert client.delete(f"/api/documents/{docid}", headers=auth(tokens["admin"])).status_code == 200
    assert app.state.store.revisions()[1] >= 5


def test_local_default_no_automatic_synthetic_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("QA_PROVIDER", raising=False)
    app = create_app(tmp_path)
    assert isinstance(app.state.provider, LocalProvider)
    seed_business(app.state.store)
    token = app.state.store.provision_user("local-user", "operator", ["PLANT-A"])
    def unavailable(*args, **kwargs):
        raise DomainError("provider_unavailable", "本地模型未连接；未使用替代模型", 503)
    monkeypatch.setattr(app.state.provider, "complete", unavailable)
    response = TestClient(app).post("/api/sessions", json=request(), headers=auth(token))
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["last_error"]["code"] == "provider_unavailable"
    assert response.json()["provider"].startswith("local-")
    monkeypatch.setenv("QA_PROVIDER", "synthetic")
    assert isinstance(configured_provider(), SyntheticProvider)
    assert isinstance(configured_provider("local"), LocalProvider)
    monkeypatch.setenv("QA_GENERATION_URL", "http://203.0.113.1:8080")
    with pytest.raises(DomainError, match="回环"):
        configured_provider("local")


def test_explicit_retry_real_service_and_unknown_block(tmp_path):
    class InterruptedSynthetic(SyntheticProvider):
        failure = "provider_unavailable"
        count = 0

        def complete(self, *args, **kwargs):
            self.count += 1
            if self.failure:
                raise DomainError(self.failure, "显式测试中断", 503)
            return super().complete(*args, **kwargs)

    provider = InterruptedSynthetic()
    app = create_app(tmp_path, provider)
    token = app.state.store.provision_user("retry-user", "admin", ["PLANT-A"])
    other = app.state.store.provision_user("retry-other", "admin", ["PLANT-A"])
    seed_business(app.state.store)
    client = TestClient(app)
    response = client.post("/api/sessions", json=request("retry-analysis"), headers=auth(token)).json()
    assert response["status"] == "blocked" and provider.count == 1
    sid = response["session_id"]
    assert client.post(f"/api/sessions/{sid}/retry", json={"action_id": "retry-denied"}, headers=auth(other)).status_code == 404
    provider.failure = None
    retried = client.post(f"/api/sessions/{sid}/retry", json={"action_id": "retry-allowed"}, headers=auth(token))
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "awaiting_confirmation"
    count = provider.count
    duplicate = client.post(f"/api/sessions/{sid}/retry", json={"action_id": "retry-allowed"}, headers=auth(token))
    assert duplicate.status_code == 200 and provider.count == count
    provider.failure = "provider_unknown"
    unknown = client.post("/api/sessions", json=request("unknown-analysis"), headers=auth(token)).json()
    assert unknown["status"] == "reconciliation_required"
    count = provider.count
    rejected = client.post(f"/api/sessions/{unknown['session_id']}/retry", json={"action_id": "unknown-retry"}, headers=auth(token))
    assert rejected.status_code == 409 and provider.count == count
    unknown_id = unknown["session_id"]
    listed = client.get(f"/api/sessions/{unknown_id}/calls", headers=auth(token))
    assert listed.status_code == 200
    call = listed.json()["calls"][0]
    assert set(call) == {"call_id", "round_id", "kind", "status", "error_code", "created_at"}
    assert client.get(f"/api/sessions/{unknown_id}/calls", headers=auth(other)).status_code == 404
    path = f"/api/sessions/{unknown_id}/calls/{call['call_id']}/resolve"
    note = "Explicit synthetic test reconciliation; checked no usable result."
    assert client.post(path, json={"note": note}, headers=auth(token)).status_code == 400
    assert client.post(path, json={"note": note, "authorize_retry_without_result": True}, headers=auth(other)).status_code == 404
    resolved = client.post(path, json={"note": note, "authorize_retry_without_result": True}, headers=auth(token))
    assert resolved.status_code == 200 and resolved.json()["status"] == "retry_authorized"
    assert provider.count == count  # Reconciliation must not automatically retry.
    provider.failure = None
    resumed = client.post(f"/api/sessions/{unknown_id}/retry", json={"action_id": "after-reconcile"}, headers=auth(token))
    assert resumed.status_code == 200 and resumed.json()["status"] == "awaiting_confirmation"

    provider.failure = "provider_unknown"
    pending = client.post("/api/sessions", json=request("receipt-analysis"), headers=auth(token)).json()
    sid = pending["session_id"]
    call = client.get(f"/api/sessions/{sid}/calls", headers=auth(token)).json()["calls"][0]
    path = f"/api/sessions/{sid}/calls/{call['call_id']}/resolve"
    receipt = {"value": {"kind": "quality_analysis", "explanation": "真实测试回执"}, "tokens": 128}
    count = provider.count
    invalid = client.post(path, json={"note": note, "result": {"value": {}, "tokens": True}}, headers=auth(token))
    assert invalid.status_code == 422
    receipt_result = client.post(path, json={"note": note, "result": receipt}, headers=auth(token))
    assert receipt_result.status_code == 200 and receipt_result.json()["status"] == "receipt_recorded"
    assert provider.count == count
    operator = app.state.store.provision_user("resolve-operator", "operator", ["PLANT-A"])
    assert client.post(path, json={"note": note, "result": receipt}, headers=auth(operator)).status_code == 403


def test_measured_calibration_and_explicit_install(setup):
    app, client, tokens = setup
    admin = app.state.store.authenticate(tokens["admin"])
    for report in original_reports("PLANT-A"):
        app.state.documents.ingest(report, admin)
    payload = {"plant_id": "PLANT-A", "use_original_cases": True, "install": False}
    assert client.post("/api/documents/calibrate", json=payload, headers=auth(tokens["analyst"])).status_code == 403
    measured = client.post("/api/documents/calibrate", json=payload, headers=auth(tokens["admin"]))
    assert measured.status_code == 200, measured.text
    result = measured.json()
    assert result["installed"] is False
    assert result["calibration"]["evaluator_kind"] == "synthetic-lexical"
    assert result["calibration"]["production_validated"] is False
    assert result["calibration"]["fit_metrics"]["case_count"] == 6
    assert result["calibration"]["eval_metrics"]["case_count"] == 6
    with app.state.store.connect() as db:
        assert db.execute("SELECT 1 FROM settings WHERE key='document_calibration'").fetchone() is None
    installed = client.post("/api/documents/calibrate", json={**payload, "install": True}, headers=auth(tokens["admin"]))
    assert installed.status_code == 200 and installed.json()["installed"] is True
    with app.state.store.connect() as db:
        assert db.execute("SELECT 1 FROM settings WHERE key='document_calibration'").fetchone()
    spoof = client.post("/api/documents/calibrate", json={**payload, "evaluator_kind": "bge-local"}, headers=auth(tokens["admin"]))
    assert spoof.status_code == 422


def test_packaged_ui_and_security_headers(setup):
    _, client, _ = setup
    page = client.get("/")
    assert page.status_code == 200 and "质量分析工作台" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "textContent" in script.text
    assert all(unsafe not in script.text for unsafe in ("localStorage", "sessionStorage", "innerHTML", "console.log"))
    assert "generation" in script.text and "AbortController" in script.text and "pagehide" in script.text
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/secret.txt").status_code == 404
    health = client.get("/api/health", headers={"Origin": "https://untrusted.invalid"})
    assert len(health.json()["graph_nodes"]) == 13
    assert health.json()["inference_checked"] is False
    assert "access-control-allow-origin" not in health.headers
