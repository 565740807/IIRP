"""Government disclosure queries, archive files and queued source checks."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from iirp.api.political_schemas import (
    PoliticalPeopleOutput,
    PoliticalReportOutput,
    PoliticalSourcesOutput,
    PoliticalTransactionsOutput,
    Source,
)
from iirp.config import settings
from iirp.db import session
from iirp.messages import NotFoundError, UserError, msg
from iirp.models import SourceObject
from iirp.models.political import PoliticalReport
from iirp.political import reads

router = APIRouter(prefix="/api/v1/political")


@router.get("/transactions", response_model=PoliticalTransactionsOutput)
def transactions(person_id: str | None = Query(default=None, max_length=36),
                 person: str | None = Query(default=None, max_length=200),
                 ticker: str | None = Query(default=None, max_length=24), source: Source | None = None,
                 direction: Literal["buy", "sell", "exchange"] | None = None,
                 transaction_from: date | None = None, transaction_to: date | None = None,
                 report_from: date | None = None, report_to: date | None = None,
                 limit: int = Query(default=20, ge=1, le=100),
                 cursor: str = Query(default="", max_length=320)):
    try:
        with session() as s:
            return reads.transactions(s, person_id=person_id, person=person, ticker=ticker, source=source,
                                      direction=direction, transaction_from=transaction_from,
                                      transaction_to=transaction_to, report_from=report_from,
                                      report_to=report_to, limit=limit, cursor=cursor)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/people", response_model=PoliticalPeopleOutput)
def people(source: Source | None = None):
    with session() as s:
        return reads.people(s, source)


@router.get("/sources", response_model=PoliticalSourcesOutput)
def sources():
    with session() as s:
        return reads.sources(s)


@router.post("/sources/{source}/refresh", response_model=PoliticalSourcesOutput, status_code=202)
def refresh(source: Source):
    from iirp.political.service import request_check

    try:
        with session() as s, s.begin():
            request_check(s, source)
        with session() as s:
            return reads.sources(s)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/reports/{report_id}", response_model=PoliticalReportOutput)
def report(report_id: str):
    try:
        with session() as s:
            return reads.report(s, report_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/reports/{report_id}/file", response_class=FileResponse)
def file(report_id: str):
    with session() as s:
        report = s.get(PoliticalReport, report_id)
        if not report:
            raise HTTPException(404, msg("political.report_not_found"))
        source = s.get(SourceObject, report.source_hash) if report.source_hash else None
        if not source:
            raise HTTPException(404, msg("source.not_found"))
        path = settings().runtime_dir / source.relative_path
        if not path.is_file():
            raise HTTPException(503, msg("source.file_missing"))
        extension = {"application/pdf": ".pdf", "text/html": ".html"}.get(source.media_type, "")
        return FileResponse(path, media_type=source.media_type,
                            filename=f"{report.source}-{report.id}{extension}")
