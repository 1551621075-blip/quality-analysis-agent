"""Local command line: no credential arguments, automatic test fallback or remote serving."""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from .api import configured_provider, create_app, data_root, provider_metadata
from .business import example_scope, seed_business
from .documents import original_reports
from .schemas import AnalyzeRequest, Confirmation, DomainError, FollowupRequest, Role, Scope
from .storage import Store


def _output(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _actor(store: Store, options):
    if getattr(options, "token_stdin", False):
        token = sys.stdin.readline(514).strip()
    else:
        token = os.environ.get("QA_TOKEN", "")
        if not token:
            if not sys.stdin.isatty():
                raise DomainError("token_required", "请设置QA_TOKEN或使用--token-stdin；不要把令牌放在命令参数中", 401)
            token = getpass.getpass("Bearer令牌（不回显）: ")
    if not token or len(token) > 512:
        raise DomainError("token_required", "需要有效Bearer令牌", 401)
    return store.authenticate(token)


def _id(value):
    return value or uuid.uuid4().hex


def _seed(app, actor, seed):
    store = app.state.store
    store.current_actor(actor)
    if actor.role != Role.ADMIN or example_scope().plant_id not in actor.plants:
        raise DomainError("forbidden", "seed需要具有示例工厂授权的admin", 403)
    with store.connect() as db:
        if db.execute("SELECT COUNT(*) FROM document_heads").fetchone()[0] or db.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0]:
            raise DomainError("seed_not_empty", "已有报告或会话，拒绝覆盖；请选择新的数据目录", 409)
    seed_business(store, seed)
    reports = [app.state.documents.ingest(report, actor)
               for report in original_reports(example_scope().plant_id)]
    return {"dataset": "原创合成业务数据与报告，非生产数据", "reports_indexed": len(reports),
            "provider": provider_metadata(app.state.provider),
            "example_scope": example_scope().model_dump(mode="json")}


def _parser():
    parser = argparse.ArgumentParser(
        prog="quality", description="中文质量分析工作台；默认local，synthetic仅显式测试使用",
        epilog="配置：QA_ROOT / QA_PROVIDER / QA_RUNTIME_CONFIG（串行CPU运行时）或"
               "QA_GENERATION_URL / QA_EMBEDDING_URL / QA_RERANK_URL；"
               "模型ID：QA_GENERATION_MODEL / QA_EMBEDDING_MODEL / QA_RERANK_MODEL。"
               "默认端点127.0.0.1:18080/18081/18082，无云端或测试桩自动回退。",
    )
    parser.add_argument("--root", type=Path, help="SQLite及制品数据目录（默认QA_ROOT或.quality-data）")
    parser.add_argument("--provider", choices=["local", "synthetic"], help="synthetic是确定性测试桩，不是LLM验收")
    commands = parser.add_subparsers(dest="command", required=True)
    user = commands.add_parser("user", help="本机管理员创建用户；令牌只显示一次且仅保存哈希")
    user.add_argument("actor_id")
    user.add_argument("--role", choices=[role.value for role in Role], required=True)
    user.add_argument("--plant", action="append", required=True, help="授权工厂，可重复")
    seed = commands.add_parser("seed", help="初始化原创合成业务数据和报告，使用选定provider建索引")
    seed.add_argument("--seed", type=int, default=17)
    seed.add_argument("--token-stdin", action="store_true")
    serve = commands.add_parser("serve", help="仅在回环地址启动API与浏览器工作台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    analyze = commands.add_parser("analyze", help="生成计划并等待确认，不自动执行SQ")
    analyze.add_argument("question")
    analyze.add_argument("--request-id")
    analyze.add_argument("--plant", required=True)
    analyze.add_argument("--product", required=True)
    analyze.add_argument("--start", required=True, help="YYYY-MM-DD")
    analyze.add_argument("--end", required=True, help="YYYY-MM-DD，结束日期不包含")
    analyze.add_argument("--metric", choices=["defect_rate", "rework_rate", "cycle_time_mean"], required=True)
    analyze.add_argument("--line", action="append", default=[])
    analyze.add_argument("--min-group-n", type=int, default=20)
    analyze.add_argument("--token-stdin", action="store_true")
    confirm = commands.add_parser("confirm", help="显式确认首轮核心SQ及计划指纹")
    confirm.add_argument("session_id")
    confirm.add_argument("--sq", action="append", required=True)
    confirm.add_argument("--plan-fingerprint", required=True)
    confirm.add_argument("--action-id")
    confirm.add_argument("--token-stdin", action="store_true")
    followup = commands.add_parser("followup", help="新轮次明确授权SQ及依赖，生成新证据")
    followup.add_argument("session_id")
    followup.add_argument("question")
    followup.add_argument("--sq", action="append", required=True)
    followup.add_argument("--request-id")
    followup.add_argument("--token-stdin", action="store_true")
    retry = commands.add_parser("retry", help="显式恢复会话；未知推理结果必须先对账")
    retry.add_argument("session_id")
    retry.add_argument("--action-id")
    retry.add_argument("--token-stdin", action="store_true")
    calls = commands.add_parser("calls", help="列出本人会话推理状态，不显示提示词")
    calls.add_argument("session_id")
    calls.add_argument("--token-stdin", action="store_true")
    reconcile = commands.add_parser("reconcile", help="本人且admin显式对账，不自动恢复")
    reconcile.add_argument("session_id")
    reconcile.add_argument("call_id")
    reconcile.add_argument("--note", required=True, help="实际核查说明，不含凭据")
    choice = reconcile.add_mutually_exclusive_group(required=True)
    choice.add_argument("--no-result", action="store_true", help="明确经核查无结果，授权后续人工重试")
    choice.add_argument("--receipt-stdin", action="store_true", help="从标准输入读取真实回执JSON，认证使用QA_TOKEN")
    demo = commands.add_parser("demo", help="空目录内演示完整计划/确认/追问；必须显式选择provider")
    demo.add_argument("--seed", type=int, default=17)
    # Accept configuration on either side of the command without subparser defaults
    # accidentally overriding explicitly supplied global options.
    for command in (user, seed, serve, analyze, confirm, followup, retry, calls, reconcile, demo):
        command.add_argument("--root", type=Path, default=argparse.SUPPRESS,
                             help="数据目录（也可放在子命令之前）")
        command.add_argument("--provider", choices=["local", "synthetic"],
                             default=argparse.SUPPRESS, help="显式选择local或synthetic测试桩")
    return parser


def _run(options):
    root = data_root(options.root)
    if options.command == "user":
        token = Store(root).provision_user(options.actor_id, options.role, options.plant)
        _output({"actor_id": options.actor_id, "role": options.role, "plants": options.plant,
                 "token": token, "notice": "令牌仅此一次显示；服务端仅保存哈希。请勿写入报告或命令参数。"})
        return
    if options.command == "demo":
        if options.provider is None and "QA_PROVIDER" not in os.environ:
            raise DomainError("explicit_provider_required", "demo必须显式--provider local或--provider synthetic（也可QA_PROVIDER）")
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            raise DomainError("demo_not_empty", "demo仅允许不存在或空的数据目录，拒绝覆盖", 409)
    if options.command == "serve":
        try:
            loopback = ipaddress.ip_address(options.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback or not 1 <= options.port <= 65535:
            raise DomainError("listen_address", "serve仅允许回环IP地址与1至65535端口")
    provider = configured_provider(options.provider)
    print(json.dumps(provider_metadata(provider), ensure_ascii=False), file=sys.stderr)
    app = create_app(root, provider)
    if options.command == "serve":
        import uvicorn
        # No credential-bearing request logs and no externally reachable default binding.
        uvicorn.run(app, host=options.host, port=options.port, access_log=False)
        return
    store, service = app.state.store, app.state.service
    if options.command == "demo":
        plant = example_scope().plant_id
        admin_token = store.provision_user("demo-admin", "admin", [plant])
        operator_token = store.provision_user("demo-operator", "operator", [plant])
        store.provision_user("demo-analyst", "analyst", [plant])
        seeded = _seed(app, store.authenticate(admin_token), options.seed)
        actor = store.authenticate(operator_token)
        prepared = service.analyze(AnalyzeRequest(request_id="demo-analysis", question="比较各产线缺陷率，并列出后续分层取证计划", scope=example_scope()), actor)
        _require_status(prepared, {"awaiting_confirmation"})
        core = [sq["sq_id"] for sq in prepared["plan"]["subquestions"] if sq["tier"] == "core"]
        confirmed = service.confirm(prepared["session_id"], Confirmation(
            action_id="demo-confirm", selected_sq_ids=core, plan_fingerprint=prepared["plan_fingerprint"]), actor)
        _require_status(confirmed, {"completed", "limited"})
        extended = [sq["sq_id"] for sq in prepared["plan"]["subquestions"] if sq["tier"] == "extended"]
        followed = None
        if extended:
            followed = service.followup(prepared["session_id"], FollowupRequest(
                request_id="demo-followup", question="明确授权第一个扩展问题及其依赖，重新取证", selected_sq_ids=[extended[0]]), actor)
            _require_status(followed, {"completed", "limited"})
        _output({"provider": provider_metadata(provider), "seed": seeded,
                 "notice": "demo用户已创建，原始令牌不输出。需要浏览器登录时另用user命令创建用户。",
                 "plan": prepared, "confirmed": confirmed, "followup": followed,
                 "followup_note": "已显式授权一个扩展SQ" if extended else "模型未提供扩展SQ，未编造追问执行"})
        return
    actor = _actor(store, options)
    if options.command == "calls":
        _output({"calls": service.calls(options.session_id, actor)})
        return
    if options.command == "reconcile":
        from .api import MAX_BODY, CallReceipt, _finite_float, _reject_constant
        receipt = None
        if options.receipt_stdin:
            raw = sys.stdin.read(MAX_BODY + 1)
            if len(raw.encode("utf-8")) > MAX_BODY:
                raise DomainError("body_too_large", "回执超过1 MiB上限")
            receipt = CallReceipt.model_validate(json.loads(
                raw, parse_constant=_reject_constant, parse_float=_finite_float)).model_dump()
        _output(service.resolve_call(options.session_id, options.call_id, actor,
                                     note=options.note, result=receipt))
        return
    if options.command == "seed":
        _output(_seed(app, actor, options.seed))
        return
    if options.command == "analyze":
        scope = Scope(plant_id=options.plant, product_id=options.product, start_date=options.start,
                      end_date=options.end, metric_id=options.metric, line_ids=options.line,
                      min_group_n=options.min_group_n)
        result = service.analyze(AnalyzeRequest(request_id=_id(options.request_id), question=options.question, scope=scope), actor)
    elif options.command == "confirm":
        result = service.confirm(options.session_id, Confirmation(action_id=_id(options.action_id),
            selected_sq_ids=options.sq, plan_fingerprint=options.plan_fingerprint), actor)
    elif options.command == "retry":
        result = service.resume(options.session_id, _id(options.action_id), actor)
    else:
        result = service.followup(options.session_id, FollowupRequest(request_id=_id(options.request_id),
            question=options.question, selected_sq_ids=options.sq), actor)
    _output(result)
    if result["status"] in {"blocked", "reconciliation_required"}:
        raise SystemExit(1)


def _require_status(result, allowed):
    if result["status"] not in allowed:
        _output(result)
        raise DomainError("demo_incomplete", "演示未完成；请查看返回状态，未自动切换provider或重试")


def main(argv=None):
    options = _parser().parse_args(argv)
    try:
        _run(options)
    except DomainError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValidationError:
        print("validation_error: 字段校验失败，请检查标识、日期、范围和格式", file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, ValueError):
        print("local_error: 本地配置或数据操作失败；未暴露路径、输入或凭据", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
