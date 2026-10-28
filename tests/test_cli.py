"""Real local CLI / SQLite contracts; synthetic invocations are not local-LLM acceptance."""
import io
import json

import pytest

from quality_analysis.cli import main
from quality_analysis.provider import SyntheticProvider
from quality_analysis.storage import Store


def invoke(args, capsys):
    main(args)
    return capsys.readouterr()


def test_user_seed_analyze_confirm_followup(tmp_path, capsys, monkeypatch):
    root = str(tmp_path / "data")
    common = ["--root", root, "--provider", "synthetic"]
    created = invoke(common + ["user", "admin-user", "--role", "admin", "--plant", "PLANT-A"], capsys)
    user = json.loads(created.out)
    token = user["token"]
    assert created.out.count(token) == 1
    assert not created.err
    store = Store(root)
    with store.connect() as db:
        saved = db.execute("SELECT * FROM users WHERE actor_id='admin-user'").fetchone()
        assert saved["token_hash"] != token
    monkeypatch.setenv("QA_TOKEN", token)
    seeded = invoke(common + ["seed"], capsys)
    assert json.loads(seeded.out)["reports_indexed"] == 8
    assert "测试桩" in seeded.err and token not in seeded.out + seeded.err
    plan = invoke(common + ["analyze", "比较各产线缺陷率", "--plant", "PLANT-A", "--product", "WIDGET-A", "--start", "2025-01-01", "--end", "2025-02-01", "--metric", "defect_rate", "--request-id", "cli-plan"], capsys)
    prepared = json.loads(plan.out)
    assert prepared["status"] == "awaiting_confirmation" and prepared["tools"] == 1
    assert "hypothesizer" not in prepared["trace"] and prepared["authorized"] == []
    confirmed = invoke(common + ["confirm", prepared["session_id"], "--sq", "core-rate", "--plan-fingerprint", prepared["plan_fingerprint"], "--action-id", "cli-confirm"], capsys)
    first = json.loads(confirmed.out)
    assert first["status"] == "completed"
    followed = invoke(common + ["followup", prepared["session_id"], "检查工单证据", "--sq", "orders", "--request-id", "cli-followup"], capsys)
    latest = json.loads(followed.out)
    assert latest["status"] == "completed"
    assert set(latest["authorized"]) == {"core-rate", "orders"}
    assert latest["round_id"] != first["round_id"]
    assert latest["provider"] == SyntheticProvider.fingerprint
    assert token not in plan.out + confirmed.out + followed.out
    with pytest.raises(SystemExit):
        main(common + ["seed"])
    assert "not_empty" in capsys.readouterr().err


def test_demo_requires_explicit_provider_and_empty_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("QA_PROVIDER", raising=False)
    root = tmp_path / "demo"
    with pytest.raises(SystemExit) as error:
        main(["--root", str(root), "demo"])
    assert error.value.code == 1
    assert "explicit_provider_required" in capsys.readouterr().err
    assert not root.exists()
    output = invoke(["demo", "--root", str(root), "--provider", "synthetic"], capsys)
    result = json.loads(output.out)
    assert result["provider"]["mode"] == "synthetic-test"
    assert result["confirmed"]["status"] == "completed"
    assert result["followup"]["status"] == "completed"
    assert '"token":' not in output.out
    store = Store(root)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    previous = store.revisions()
    with pytest.raises(SystemExit):
        main(["--root", str(root), "--provider", "synthetic", "demo"])
    assert "demo_not_empty" in capsys.readouterr().err
    assert store.revisions() == previous


def test_cli_refuses_remote_serving_without_creating_store(tmp_path, capsys):
    root = tmp_path / "remote"
    with pytest.raises(SystemExit):
        main(["--root", str(root), "serve", "--host", "0.0.0.0"])
    assert "回环" in capsys.readouterr().err
    assert not root.exists()


def test_cli_stdin_token_and_sanitized_validation(tmp_path, capsys, monkeypatch):
    root = tmp_path / "stdin"
    store = Store(root)
    token = store.provision_user("operator", "operator", ["PLANT-A"])
    monkeypatch.delenv("QA_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
    with pytest.raises(SystemExit):
        main(["--root", str(root), "--provider", "synthetic", "analyze", "质量问题", "--plant", "PLANT-A", "--product", "WIDGET-A", "--start", "SENSITIVE_BAD_DATE", "--end", "2025-02-01", "--metric", "defect_rate", "--token-stdin"])
    output = capsys.readouterr()
    assert "validation_error" in output.err
    assert "SENSITIVE_BAD_DATE" not in output.out + output.err
    assert token not in output.out + output.err


def test_cli_no_scope_guessing_and_duplicate_user(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["--root", str(tmp_path), "analyze", "比较质量问题"])
    assert error.value.code == 2
    capsys.readouterr()
    args = ["--root", str(tmp_path), "user", "unique-user", "--role", "analyst", "--plant", "PLANT-A"]
    first = json.loads(invoke(args, capsys).out)
    with pytest.raises(SystemExit):
        main(args)
    output = capsys.readouterr()
    assert "user_exists" in output.err and first["token"] not in output.out + output.err


def test_cli_explicit_calls_reconciliation_and_retry(tmp_path, capsys, monkeypatch):
    from quality_analysis.api import create_app
    from quality_analysis.business import example_scope, seed_business
    from quality_analysis.schemas import AnalyzeRequest, DomainError

    class UnknownSynthetic(SyntheticProvider):
        def complete(self, *args, **kwargs):
            raise DomainError("provider_unknown", "Explicit synthetic interruption", 503)

    app = create_app(tmp_path, UnknownSynthetic())
    token = app.state.store.provision_user("cli-reconcile", "admin", ["PLANT-A"])
    seed_business(app.state.store)
    session = app.state.service.analyze(AnalyzeRequest(request_id="unknown-cli", question="核查调用失败", scope=example_scope()), app.state.store.authenticate(token))
    monkeypatch.setenv("QA_TOKEN", token)
    common = ["--root", str(tmp_path), "--provider", "synthetic"]
    calls = json.loads(invoke(common + ["calls", session["session_id"]], capsys).out)["calls"]
    assert len(calls) == 1 and "prompt" not in calls[0]
    resolved = invoke(common + ["reconcile", session["session_id"], calls[0]["call_id"], "--note", "Explicit test: checked unavailable result, authorize manual retry.", "--no-result"], capsys)
    assert json.loads(resolved.out)["status"] == "retry_authorized"
    with app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
    retried = invoke(common + ["retry", session["session_id"], "--action-id", "manual-cli-retry"], capsys)
    assert json.loads(retried.out)["status"] == "awaiting_confirmation"
    assert token not in resolved.out + resolved.err + retried.out + retried.err


def test_cli_default_local_serve_configuration(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("QA_PROVIDER", raising=False)
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    output = invoke(["--root", str(tmp_path), "serve"], capsys)
    app, kwargs = calls[0]
    assert kwargs == {"host": "127.0.0.1", "port": 8000, "access_log": False}
    assert app.state.provider.fingerprint.startswith("local-")
    assert "本地模型" in output.err and "回退" in output.err
