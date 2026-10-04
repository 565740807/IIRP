import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from iirp import __version__
from iirp.config import ROOT, settings
from iirp.contracts import FeedOutput, GenericOutput, HomeOutput, SystemOutput
from iirp.db import session
from iirp.models import Coverage, Job, Policy, SourceObject
from iirp.providers import sec_configured
from iirp.queue import (
    control,
    create_job,
    ensure_defaults,
    job_view,
    policy_view,
    update_policy,
    worker_view,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionRequest(StrictModel):
    kind: Literal["fixture_check", "market_probe", "sec_probe"]
    ticker: str = Field(default="AAPL", pattern=r"^[A-Za-z0-9^][A-Za-z0-9.^=-]{0,14}$")


class ActionRequest(StrictModel):
    action: Literal["pause", "resume", "cancel", "retry"]


class PolicyRequest(StrictModel):
    sec_enabled: bool


class JobView(BaseModel):
    id: str
    kind: str
    title: str
    status: str
    trigger: str
    progress_done: int
    progress_total: int
    checkpoint: dict[str, Any]
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    requested_action: str | None
    control_version: int
    result: dict[str, Any] | None
    control_notice: str | None = None


class CollectionResponse(BaseModel):
    job_id: str
    reused: bool
    job: JobView


class WorkerView(BaseModel):
    online: bool
    last_seen: datetime | None


class JobsResponse(BaseModel):
    items: list[JobView]
    worker: WorkerView


class PolicyView(BaseModel):
    sec_enabled: bool
    version: int
    updated_at: datetime
    next_run_at: datetime | None
    scope: str


@asynccontextmanager
async def lifespan(_app):
    # Readiness reports DB/migration failure; web can still explain an outage.
    try:
        ensure_defaults()
        from iirp.lifecycle import defaults

        with session() as s, s.begin():
            defaults(s)
    except SQLAlchemyError:
        pass
    yield


app = FastAPI(title="IIRP V1", version=__version__, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
        origin = request.headers.get("origin")
        if request.headers.get("x-iirp-client") not in ("web", "cli"):
            return JSONResponse({"detail": "缺少本地客户端标识。"}, status_code=403)
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != request.headers.get(
                "host"
            ):
                return JSONResponse({"detail": "只允许同源操作。"}, status_code=403)
        from iirp.import_limits import request_limit

        limit = request_limit(request.url.path)

        def too_large(size):
            return JSONResponse(
                {
                    "detail": f"本次请求至少 {size:,} 字节，当前接口最多 {limit:,} 字节（含 JSON 包装与转义）。资料未保存；请检查是否重复粘贴，原文可继续修改后重试。",
                    "request_bytes": size,
                    "limit_bytes": limit,
                },
                status_code=413,
            )

        try:
            declared = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"detail": "请求长度格式无效，请重新发送。"}, status_code=400)
        if declared > limit:
            return too_large(declared)
        chunks, received = [], 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                return too_large(received)
            chunks.append(chunk)
        # Starlette's cached request replays this bounded body to the endpoint.
        # Count the real stream, including requests without Content-Length.
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(SQLAlchemyError)
async def db_error(_request, _exc):
    if getattr(getattr(_exc, "orig", None), "sqlstate", None) == "57014":
        return JSONResponse(
            {"detail": "数据库响应超时，本次操作尚未确认。已保存研究仍保留，请稍后重试。", "code": "database_timeout"},
            status_code=503, headers={"Retry-After": "3"},
        )
    if getattr(getattr(_exc, "orig", None), "sqlstate", None) in {"55P03", "40001", "40P01"}:
        return JSONResponse(
            {"detail": "数据正在提交，本次操作尚未确认。请稍后重试；已保存研究与任务状态保留。"},
            status_code=503,
            headers={"Retry-After": "1"},
        )
    return JSONResponse(
        {"detail": "数据库暂不可用；操作尚未确认，请恢复连接后重试。"}, status_code=503
    )


@app.get("/health/live")
def live():
    return {"status": "alive", "version": __version__}


@app.get("/health/ready")
def ready():
    try:
        with session() as s:
            revision = s.scalar(text("SELECT version_num FROM alembic_version"))
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        expected = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
        if revision != expected:
            raise ValueError("migration mismatch")
        return {"status": "ready", "migration": revision}
    except (SQLAlchemyError, ValueError):
        return JSONResponse(
            {"status": "not_ready", "reason": "数据库或迁移版本未就绪。"}, status_code=503
        )


@app.get("/api/v1/diagnostics/policy", response_model=PolicyView)
def get_policy():
    with session() as s:
        return policy_view(s.get(Policy, 1))


@app.patch("/api/v1/diagnostics/policy", response_model=PolicyView)
def patch_policy(body: PolicyRequest):
    if body.sec_enabled and not sec_configured():
        raise HTTPException(409, "请先在本地配置含真实联系邮箱的 SEC User-Agent。")
    if body.sec_enabled and settings().mode != "development":
        raise HTTPException(409, "正式采集调度将在 P2 接入；P1 不以测试采样代替真实采集。")
    return policy_view(update_policy(body.sec_enabled))


@app.get("/api/v1/jobs", response_model=JobsResponse)
def get_jobs():
    with session() as s:
        jobs = s.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)).all()
        return {"items": [job_view(job) for job in jobs], "worker": worker_view(s)}


@app.post("/api/v1/diagnostics/collections", status_code=202, response_model=CollectionResponse)
def collection(body: CollectionRequest):
    if body.kind == "sec_probe" and not sec_configured():
        raise HTTPException(409, "SEC 联系邮箱尚未配置。")
    if settings().mode != "development":
        raise HTTPException(409, "测试采样仅在开发模式可用；正式采集在 P2 接入。")
    target = {"ticker": body.ticker.upper()} if body.kind == "market_probe" else {}
    try:
        job, reused = create_job(body.kind, target)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id, "reused": reused, "job": job_view(job)}


@app.post("/api/v1/jobs/{job_id}/actions", response_model=JobView)
def action(job_id: str, body: ActionRequest):
    try:
        return job_view(control(job_id, body.action))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/home", response_model=HomeOutput)
def home():
    from iirp.local_reads import home

    return home()


@app.get("/api/v1/feed", response_model=FeedOutput)
def feed(session_id: str = "", cursor: str = "", type: str = "all", order: str = "transaction"):
    from iirp.local_reads import feed

    try:
        return feed(session_id, cursor, type, order)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/demo")
def demo():
    return {
        "label": "合成测试样例 · 非真实交易",
        "groups": [
            {
                "id": "demo-acme",
                "company": "示例科技（合成）",
                "ticker": "DEMO",
                "accepted_at": "2026-09-04T21:12:00Z",
                "transaction_dates": "2026-09-02—2026-09-03",
                "owners": 2,
                "filings": 2,
                "transactions": [
                    {
                        "id": "demo-t1",
                        "owner": "Example Owner A",
                        "code": "P",
                        "kind": "普通股买入",
                        "shares": 1000,
                        "price": 20,
                        "transaction_date": "2026-09-02",
                        "accepted_at": "2026-09-04T21:12:00Z",
                    },
                    {
                        "id": "demo-t2",
                        "owner": "Example Owner B",
                        "code": "M",
                        "kind": "衍生品行权",
                        "shares": 500,
                        "price": None,
                        "transaction_date": "2026-09-03",
                        "accepted_at": "2026-09-04T20:40:00Z",
                    },
                ],
            },
            {
                "id": "demo-north",
                "company": "示例工业（合成）",
                "ticker": "TEST",
                "accepted_at": "2026-09-04T19:30:00Z",
                "transaction_dates": "2026-09-01",
                "owners": 1,
                "filings": 1,
                "transactions": [
                    {
                        "id": "demo-t3",
                        "owner": "Example Owner C",
                        "code": "S",
                        "kind": "普通股卖出",
                        "shares": 2000,
                        "price": 12.5,
                        "transaction_date": "2026-09-01",
                        "accepted_at": "2026-09-04T19:30:00Z",
                    }
                ],
            },
        ],
    }


@app.get("/api/v1/providers", response_model=GenericOutput)
def providers():
    from iirp.local_reads import providers

    return providers()


@app.get("/api/v1/diagnostics/coverage")
def coverage():
    with session() as s:
        rows = s.scalars(select(Coverage).order_by(Coverage.updated_at.desc()).limit(100)).all()
        return {
            "items": [
                {
                    k: getattr(row, k)
                    for k in (
                        "provider",
                        "target",
                        "status",
                        "message",
                        "updated_at",
                        "source_hash",
                    )
                }
                for row in rows
            ],
            "notice": "这是测试来源能力记录。样本成功不等于正式历史覆盖完整。",
        }


@app.get("/api/v1/system", response_model=SystemOutput)
def system():
    from iirp.system_status import system_database_state

    worker, migration = system_database_state()
    return {
        "version": __version__,
        "mode": settings().mode,
        "worker": worker,
        "migration": migration,
        "automatic_collection_scope": "最新数据优先；历史默认 3 个月可修改，各来源可独立暂停",
        "storage": __import__("iirp.maintenance", fromlist=["storage_state"]).storage_state(),
    }


@app.get("/api/v1/search", response_model=GenericOutput)
def search(q: str = ""):
    from iirp.local_reads import search

    return search(q)


@app.get("/api/v1/sources/{digest}")
def source(digest: str):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise HTTPException(404, "来源不存在。")
    with session() as s:
        row = s.get(SourceObject, digest)
        if not row:
            raise HTTPException(404, "来源不存在。")
        path = settings().runtime_dir / row.relative_path
        if not path.is_file():
            raise HTTPException(503, "来源文件缺失，需要恢复核对。")
        return FileResponse(path, media_type="application/octet-stream", filename=digest)


from iirp.research_api import router as research_router  # noqa: E402

app.include_router(research_router)

from iirp.event_api import router as event_router  # noqa: E402

app.include_router(event_router)

from iirp.feed_updates import router as feed_updates_router  # noqa: E402

app.include_router(feed_updates_router)

DIST = ROOT / "frontend/dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith(("api/", "health/", "assets/")):
        raise HTTPException(404, "接口不存在。")
    if not (DIST / "index.html").is_file():
        return JSONResponse({"detail": "前端尚未构建，请运行 npm run build。"}, status_code=503)
    return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
