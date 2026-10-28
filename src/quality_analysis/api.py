import base64
import binascii
import json
import math
import os
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .provider import LocalProvider, SyntheticProvider
from .retrieval_calibration import CalibrationCase, calibrate, original_calibration_cases
from .schemas import (
    Actor,
    AnalyzeRequest,
    Confirmation,
    DocumentInput,
    DomainError,
    FollowupRequest,
    Identifier,
    Role,
    Scope,
    StrictModel,
)
from .service import NODES, AnalysisService
from .storage import Store

MAX_BODY = 1024 * 1024
LOCAL_DEFAULTS = {
    "generation_url": "http://127.0.0.1:18080",
    "embedding_url": "http://127.0.0.1:18081",
    "rerank_url": "http://127.0.0.1:18082",
    "generation_model": "qwen3-4b-instruct-2507",
    "embedding_model": "bge-m3",
    "rerank_model": "bge-reranker-v2-m3",
}


def configured_provider(choice: str | None = None):
    selected = choice if choice is not None else os.environ.get("QA_PROVIDER", "local")
    if selected == "synthetic":
        return SyntheticProvider()
    if selected != "local":
        raise DomainError("provider_choice", "provider仅支持local或显式synthetic")
    if os.environ.get("QA_RUNTIME_CONFIG"):
        from .managed_provider import ManagedLocalProvider
        return ManagedLocalProvider(os.environ["QA_RUNTIME_CONFIG"])
    return LocalProvider(**{
        name: os.environ.get("QA_" + name.upper(), default)
        for name, default in LOCAL_DEFAULTS.items()
    })


def data_root(root: str | Path | None = None) -> Path:
    return Path(root if root is not None else os.environ.get("QA_ROOT", ".quality-data"))


def provider_metadata(provider) -> dict:
    synthetic = isinstance(provider, SyntheticProvider)
    result = {
        "fingerprint": provider.fingerprint,
        "mode": "synthetic-test" if synthetic else "local",
        "label": "合成测试桩；不是本地LLM/BGE验收" if synthetic else "本地模型；不自动回退测试桩",
    }
    if isinstance(provider, LocalProvider):
        result["endpoints"] = {
            name: getattr(provider, name) for name in LOCAL_DEFAULTS
        }
    return result


def _error(code: str, message: str, status: int) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite")
    return number


def _reject_constant(value: str):
    raise ValueError("non-finite")


class BoundedJSON:
    """Bound the actual ASGI stream, not just the untrusted Content-Length header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = False

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; "
                     b"style-src 'self'; connect-src 'self'; img-src 'self'; "
                     b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                ]
            await send(message)

        if not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, secure_send)
        headers = dict(scope["headers"])
        try:
            declared = int(headers.get(b"content-length", b"0"))
            if declared < 0:
                raise ValueError
        except ValueError:
            return await _error("invalid_body", "请求体格式无效", 400)(scope, receive, secure_send)
        if declared > MAX_BODY:
            return await _error("body_too_large", "请求体超过1 MiB上限", 413)(scope, receive, secure_send)
        content = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(content) + len(chunk) > MAX_BODY:
                return await _error("body_too_large", "请求体超过1 MiB上限", 413)(scope, receive, secure_send)
            content.extend(chunk)
            if not message.get("more_body", False):
                break
        if content:
            media = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if media != b"application/json":
                return await _error("json_required", "请使用application/json请求体", 415)(scope, receive, secure_send)
            try:
                parsed = json.loads(content, parse_constant=_reject_constant, parse_float=_finite_float)
                if not isinstance(parsed, dict):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return await _error("invalid_json", "JSON必须为有效对象且数字必须有限", 400)(scope, receive, secure_send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(content), "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, secure_send)
        except Exception:
            # Uvicorn otherwise logs traceback messages, which may embed source inputs.
            # Keep API failures generic and never serialize exception/request objects.
            if not started:
                await _error("internal_error", "服务暂时无法完成请求；未暴露内部详情", 500)(scope, replay, secure_send)


class RetryRequest(StrictModel):
    action_id: Identifier


class CallReceipt(StrictModel):
    value: dict
    tokens: int = Field(gt=0, strict=True)


class ReconcileRequest(StrictModel):
    note: str = Field(min_length=10, max_length=1000)
    result: CallReceipt | None = None
    authorize_retry_without_result: bool = False


class SearchRequest(StrictModel):
    query: str = Field(min_length=2, max_length=2000)
    scope: Scope


class UploadRequest(StrictModel):
    document_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    plant_id: Identifier
    shared_with: list[Identifier] = Field(default_factory=list, max_length=100)
    summaries: dict[str, str]
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1, max_length=MAX_BODY)


class ACLRequest(StrictModel):
    shared_with: list[Identifier] = Field(max_length=100)


class CalibrationRequest(StrictModel):
    plant_id: Identifier
    cases: list[CalibrationCase] | None = Field(default=None, min_length=4, max_length=100)
    use_original_cases: bool = False
    install: bool = False


def create_app(root=None, provider=None) -> FastAPI:
    from .documents import DocumentStore
    from .verifier import Verifier

    store = Store(data_root(root))
    provider = configured_provider() if provider is None else provider
    documents = DocumentStore(store, provider)
    service = AnalysisService(store, provider, documents, Verifier(store, documents))
    app = FastAPI(title="质量分析工作台", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.service, app.state.documents = store, service, documents
    app.state.provider = provider
    app.add_middleware(BoundedJSON)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return _error(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        # Never include Pydantic's input, ctx, exception text or request body.
        return _error("validation_error", "字段校验失败，请检查必填项、范围与格式", 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return _error("internal_error", "服务暂时无法完成请求；未暴露内部详情", 500)

    def actor(request: Request) -> Actor:
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 512 or token.strip() != token:
            raise DomainError("unauthorized", "需要有效Bearer令牌", 401)
        return store.authenticate(token)

    Authorized = Annotated[Actor, Depends(actor)]

    def editor(current: Authorized) -> Actor:
        if current.role not in {Role.ANALYST, Role.ADMIN}:
            raise DomainError("forbidden", "报告修改仅允许analyst/admin", 403)
        return current

    def administrator(current: Authorized) -> Actor:
        if current.role != Role.ADMIN:
            raise DomainError("forbidden", "此操作仅允许admin", 403)
        return current

    Editor = Annotated[Actor, Depends(editor)]
    Administrator = Annotated[Actor, Depends(administrator)]

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(files("quality_analysis").joinpath("templates/index.html").read_text("utf-8"))

    @app.get("/static/{name}")
    def asset(name: str):
        media = {"app.js": "application/javascript", "style.css": "text/css"}
        if name not in media:
            return _error("not_found", "资源不存在", 404)
        return Response(files("quality_analysis").joinpath("static", name).read_bytes(), media_type=media[name])

    @app.get("/api/health")
    def health():
        return {"status": "ok", "provider": provider_metadata(provider), "graph_nodes": NODES,
                "inference_checked": False, "note": "仅进程健康；不代表本地模型推理已验收"}

    @app.get("/api/me")
    def me(current: Authorized):
        from .business import example_scope
        example = example_scope()
        if hasattr(example, "model_dump"):
            example = example.model_dump(mode="json")
        return {"actor": current.model_dump(mode="json"), "provider": provider_metadata(provider),
                "example_scope": example if example["plant_id"] in current.plants else None}

    @app.get("/api/sessions")
    def sessions(current: Authorized):
        return {"sessions": service.list_sessions(current)}

    @app.post("/api/sessions")
    def analyze(payload: AnalyzeRequest, current: Authorized):
        return service.analyze(payload, current)

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str, current: Authorized):
        return service.get(session_id, current)

    @app.post("/api/sessions/{session_id}/confirm")
    def confirm(session_id: str, payload: Confirmation, current: Authorized):
        return service.confirm(session_id, payload, current)

    @app.post("/api/sessions/{session_id}/followup")
    def followup(session_id: str, payload: FollowupRequest, current: Authorized):
        return service.followup(session_id, payload, current)

    @app.post("/api/sessions/{session_id}/retry")
    def retry(session_id: str, payload: RetryRequest, current: Authorized):
        return service.resume(session_id, payload.action_id, current)

    @app.get("/api/sessions/{session_id}/calls")
    def calls(session_id: str, current: Authorized):
        return {"calls": service.calls(session_id, current)}

    @app.post("/api/sessions/{session_id}/calls/{call_id}/resolve")
    def reconcile(session_id: str, call_id: str, payload: ReconcileRequest, current: Administrator):
        if payload.authorize_retry_without_result == (payload.result is not None):
            raise DomainError("reconcile_choice", "必须明确提交真实回执或声明无结果授权重试，不能同时选择")
        return service.resolve_call(session_id, call_id, current, note=payload.note,
            result=payload.result.model_dump() if payload.result is not None else None)

    @app.get("/api/sessions/{session_id}/audit")
    def audit(session_id: str, current: Authorized):
        return {"audit": service.audit(session_id, current)}

    @app.post("/api/documents/ingest")
    def ingest(payload: DocumentInput, current: Editor):
        return documents.ingest(payload, current)

    @app.get("/api/documents/{document_id}/chunks/{chunk_id}")
    def drill(document_id: str, chunk_id: str, current: Authorized):
        return documents.drill(document_id, chunk_id, current)

    @app.post("/api/documents/search")
    def search(payload: SearchRequest, current: Authorized):
        return documents.search(payload.query, payload.scope, current)

    @app.post("/api/documents/upload")
    async def upload(payload: UploadRequest, current: Editor):
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise DomainError("invalid_upload", "文件编码无效") from None
        from .documents import parse_upload
        metadata = payload.model_dump(exclude={"filename", "content_base64"})
        text = await run_in_threadpool(parse_upload, payload.filename, content)
        document = DocumentInput(**metadata, text=text)
        return await run_in_threadpool(documents.ingest, document, current)

    @app.post("/api/documents/calibrate")
    def calibration(payload: CalibrationRequest, current: Administrator):
        if payload.plant_id not in current.plants:
            raise DomainError("forbidden", "校准工厂未授权", 403)
        if payload.use_original_cases == (payload.cases is not None):
            raise DomainError("calibration_cases", "请选择原创合成标签或提供独立fit/eval标签，不能同时选择")
        cases = original_calibration_cases(payload.plant_id) if payload.use_original_cases else payload.cases
        if any(case.scope.plant_id != payload.plant_id for case in cases):
            raise DomainError("calibration_scope", "所有校准样例必须属于明确选择的工厂")
        result = calibrate(documents, cases, current,
            evaluator_kind="synthetic-lexical" if isinstance(provider, SyntheticProvider) else "local-model",
            dataset_kind="original-synthetic" if payload.use_original_cases else "caller-labeled")
        if payload.install:
            documents.install_calibration(result, current)
        return {"calibration": result.model_dump(mode="json"), "installed": payload.install,
                "note": "fit选参、独立eval评估；仅此provider/身份/ACL/索引快照适用，不是生产验收"}

    @app.patch("/api/documents/{document_id}/acl")
    def acl(document_id: str, payload: ACLRequest, current: Administrator):
        return documents.set_acl(document_id, payload.shared_with, current)

    @app.delete("/api/documents/{document_id}")
    def delete(document_id: str, current: Administrator):
        return documents.delete(document_id, current)

    return app
