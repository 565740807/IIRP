import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from iirp import __version__
from iirp.api.insider_schemas import FeedOutput
from iirp.api.schemas import GenericOutput, HomeOutput, SearchOutput, SystemOutput
from iirp.config import ROOT, settings
from iirp.db import session
from iirp.jobs.job_views import job_view, list_jobs, policy_view
from iirp.jobs.providers import sec_configured, sec_user_agent_state
from iirp.jobs.queue import control, create_job, ensure_defaults, update_policy
from iirp.messages import UserError, decode, msg
from iirp.models import Coverage, Job, Policy, SourceObject


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionRequest(StrictModel):
    kind: Literal["fixture_check", "market_probe", "sec_probe"]
    ticker: str = Field(default="AAPL", pattern=r"^[A-Za-z0-9^][A-Za-z0-9.^=-]{0,14}$")


class ActionRequest(StrictModel):
    action: Literal["pause", "resume", "cancel", "retry"]


class PolicyRequest(StrictModel):
    sec_enabled: bool


class JobSummary(BaseModel):
    """List row: scalar fields only; checkpoint/result/target stay in the detail."""
    id: str
    kind: str
    title: str
    status: str
    trigger: str
    progress_done: int
    progress_total: int
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    requested_action: str | None
    control_version: int
    control_notice: str | None = None


class JobView(JobSummary):
    checkpoint: dict[str, Any]
    result: dict[str, Any] | None


class JobDetailView(JobView):
    target: dict[str, Any]


class CollectionResponse(BaseModel):
    job_id: str
    reused: bool
    job: JobView


class WorkerView(BaseModel):
    online: bool
    last_seen: datetime | None


class JobsResponse(BaseModel):
    items: list[JobSummary]
    worker: WorkerView
    next_cursor: str | None = None


class PolicyView(BaseModel):
    sec_enabled: bool
    version: int
    updated_at: datetime
    next_run_at: datetime | None
    scope: str


@asynccontextmanager
async def lifespan(_app):
    # Build the local exchange calendar before serving stock quote comparisons.
    from iirp.market.stock_quotes import stock_session

    stock_session(datetime.now().astimezone())
    # Readiness reports DB/migration failure; web can still explain an outage.
    try:
        ensure_defaults()
        from iirp.jobs.batches import defaults

        with session() as s, s.begin():
            defaults(s)
        from iirp.insider.feed_index import ensure_cluster

        with session() as s, s.begin():
            ensure_cluster(s)
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
            return JSONResponse({"detail": decode(msg("http.client_header_missing"))}, status_code=403)
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != request.headers.get(
                "host"
            ):
                return JSONResponse({"detail": decode(msg("http.same_origin_only"))}, status_code=403)
        from iirp.api.limits import request_limit

        limit = request_limit(request.url.path)

        def too_large(size):
            return JSONResponse(
                {
                    "detail": decode(msg("http.request_too_large", size=size, limit=limit)),
                    "request_bytes": size,
                    "limit_bytes": limit,
                },
                status_code=413,
            )

        try:
            declared = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"detail": decode(msg("http.content_length_invalid"))}, status_code=400)
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
            {"detail": decode(msg("http.database_timeout")), "code": "database_timeout"},
            status_code=503, headers={"Retry-After": "3"},
        )
    if getattr(getattr(_exc, "orig", None), "sqlstate", None) in {"55P03", "40001", "40P01"}:
        return JSONResponse(
            {"detail": decode(msg("http.database_busy"))},
            status_code=503,
            headers={"Retry-After": "1"},
        )
    return JSONResponse(
        {"detail": decode(msg("http.database_unavailable"))}, status_code=503
    )


@app.exception_handler(HTTPException)
async def message_error(request, exc):
    """Errors raised with an encoded message return it as an object in ``detail``."""
    message = decode(exc.detail)
    if message is None:
        return await http_exception_handler(request, exc)
    return JSONResponse({"detail": message}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def message_validation_error(request, exc):
    """A validator's UserError replaces pydantic's English text with its message."""
    errors = []
    for error in exc.errors():
        cause = (error.get("ctx") or {}).get("error")
        if isinstance(cause, UserError):
            error = {**error, "msg": decode(str(cause)), "ctx": {}}
        errors.append(error)
    return await request_validation_exception_handler(request, RequestValidationError(errors))


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
            {"status": "not_ready", "reason": "database or migration revision not ready"}, status_code=503
        )


@app.get("/api/v1/diagnostics/policy", response_model=PolicyView)
def get_policy():
    with session() as s:
        return policy_view(s.get(Policy, 1))


@app.patch("/api/v1/diagnostics/policy", response_model=PolicyView)
def patch_policy(body: PolicyRequest):
    if body.sec_enabled and not sec_configured():
        raise HTTPException(409, msg("sec.user_agent_required"))
    if body.sec_enabled and settings().mode != "development":
        raise HTTPException(409, msg("diagnostics.development_only"))
    return policy_view(update_policy(body.sec_enabled))


@app.get("/api/v1/jobs", response_model=JobsResponse)
def get_jobs(limit: int = Query(default=20, ge=1, le=100), cursor: str = Query(default="", max_length=80)):
    try:
        return list_jobs(limit, cursor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/jobs/{job_id}", response_model=JobDetailView)
def get_job(job_id: str):
    with session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(404, msg("job.not_found"))
        return {**job_view(job), "target": job.target}


@app.post("/api/v1/diagnostics/collections", status_code=202, response_model=CollectionResponse)
def collection(body: CollectionRequest):
    if body.kind == "sec_probe" and not sec_configured():
        raise HTTPException(409, msg("sec.contact_missing"))
    if settings().mode != "development":
        raise HTTPException(409, msg("diagnostics.sample_development_only"))
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
    from iirp.api.reads import home

    return home()


@app.get("/api/v1/feed", response_model=FeedOutput)
def feed(session_id: str = "", cursor: str = "", type: str = "all", order: str = "transaction",
         index: str = Query(default="all", pattern="^(all|sp500|nasdaq100)$")):
    from iirp.api.reads import feed

    try:
        return feed(session_id, cursor, type, order, index)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/providers", response_model=GenericOutput)
def providers():
    from iirp.api.reads import providers

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
            "notice": msg("diagnostics.coverage_notice"),
        }


@app.get("/api/v1/system", response_model=SystemOutput)
def system():
    from iirp.storage.status import system_database_state

    worker, migration = system_database_state()
    return {
        "version": __version__,
        "mode": settings().mode,
        "worker": worker,
        "migration": migration,
        "automatic_collection_scope": msg("system.collection_scope"),
        "sec_user_agent": sec_user_agent_state(),
        "storage": __import__("iirp.storage.maintenance", fromlist=["storage_state"]).storage_state(),
    }


@app.post("/api/v1/system/storage/exact", response_model=GenericOutput, status_code=202)
def storage_exact():
    """Start the exact directory walk (about 1.3 million files); requests coalesce."""
    from iirp.storage.inventory import request_exact

    return {"data": request_exact()}


@app.get("/api/v1/search", response_model=SearchOutput)
def search(q: str = ""):
    from iirp.api.reads import search

    return search(q)


@app.get("/api/v1/sources/{digest}")
def source(digest: str):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise HTTPException(404, msg("source.not_found"))
    with session() as s:
        row = s.get(SourceObject, digest)
        if not row:
            raise HTTPException(404, msg("source.not_found"))
        path = settings().runtime_dir / row.relative_path
        if not path.is_file():
            raise HTTPException(503, msg("source.file_missing"))
        return FileResponse(path, media_type="application/octet-stream", filename=digest)


from iirp.api.research import router as research_router  # noqa: E402

app.include_router(research_router)

from iirp.api.events import router as event_router  # noqa: E402

app.include_router(event_router)

from iirp.api.insider import router as insider_router  # noqa: E402

app.include_router(insider_router)

from iirp.api.overview import router as overview_router  # noqa: E402

app.include_router(overview_router)

from iirp.api.health import router as health_router  # noqa: E402

app.include_router(health_router)

DIST = ROOT / "frontend/dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith(("api/", "health/", "assets/")):
        raise HTTPException(404, msg("http.not_found"))
    if not (DIST / "index.html").is_file():
        return JSONResponse({"detail": decode(msg("http.frontend_not_built"))}, status_code=503)
    return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
