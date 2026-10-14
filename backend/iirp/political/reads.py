"""Local-only government disclosure reads; amendments count once."""
import base64
import binascii
import json
from datetime import date

from sqlalchemy import func, or_, select, tuple_

from iirp.api.political_schemas import PoliticalPersonView, PoliticalTransactionView
from iirp.messages import NotFoundError, UserError
from iirp.models import ACTIVE, Job, SourcePoll
from iirp.models.political import PoliticalPerson, PoliticalReport, PoliticalTransaction


def active_reports():
    return (PoliticalReport.status != "superseded",
            PoliticalReport.amendment_status != "unresolved")


def transactions(s, *, person_id=None, person=None, ticker=None, source=None, direction=None,
                 transaction_from=None, transaction_to=None, report_from=None, report_to=None,
                 limit=20, cursor=""):
    for start, end in ((transaction_from, transaction_to), (report_from, report_to)):
        if start and end and start > end:
            raise UserError("political.date_range_invalid")
    query = (select(PoliticalTransaction, PoliticalReport, PoliticalPerson)
             .join(PoliticalReport, PoliticalTransaction.report_id == PoliticalReport.id)
             .join(PoliticalPerson, PoliticalReport.person_id == PoliticalPerson.id)
             .where(*active_reports()))
    if person_id:
        query = query.where(PoliticalPerson.id == person_id)
    if person:
        query = query.where(PoliticalPerson.name.icontains(person, autoescape=True))
    if ticker:
        query = query.where(PoliticalTransaction.ticker == ticker.strip().upper())
    if source:
        query = query.where(PoliticalReport.source == source)
    if direction:
        types = {"buy": ("purchase",), "sell": ("sale", "partial_sale"),
                 "exchange": ("exchange",)}[direction]
        query = query.where(PoliticalTransaction.transaction_type.in_(types))
    for column, start, end in ((PoliticalTransaction.transaction_date, transaction_from, transaction_to),
                               (func.coalesce(PoliticalReport.report_date, PoliticalReport.filed_date),
                                report_from, report_to)):
        if start:
            query = query.where(column >= start)
        if end:
            query = query.where(column <= end)
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    # Report date first, then fixed identifiers: ties and missing dates page predictably.
    sort = (func.coalesce(PoliticalReport.report_date, PoliticalReport.filed_date, date.min), PoliticalReport.id,
            PoliticalTransaction.page, PoliticalTransaction.row_number, PoliticalTransaction.id)
    if cursor:
        try:
            key = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (not isinstance(key, list) or len(key) != 5 or not isinstance(key[1], str)
                    or not isinstance(key[2], int) or not isinstance(key[3], int)
                    or not isinstance(key[4], str)):
                raise ValueError
            key[0] = date.fromisoformat(key[0])
        except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
            raise UserError("political.cursor_invalid") from exc
        query = query.where(tuple_(*sort) < tuple_(*key))
    rows = s.execute(query.order_by(*(column.desc() for column in sort)).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        transaction, report, _person = rows[limit - 1]
        key = [(report.report_date or report.filed_date or date.min).isoformat(), report.id,
               transaction.page, transaction.row_number, transaction.id]
        next_cursor = base64.urlsafe_b64encode(json.dumps(key).encode()).decode()
    return {"items": [{**PoliticalTransactionView.model_validate(transaction).model_dump(),
                       "source": report.source, "filed_date": report.filed_date,
                       "report_date": report.report_date, "report_date_source": report.report_date_source,
                       "person": PoliticalPersonView.model_validate(person)}
                      for transaction, report, person in rows[:limit]],
            "total": total, "next_cursor": next_cursor}


def people(s, source=None):
    reports = (select(PoliticalReport.person_id, func.count().label("report_count"),
                      func.max(func.coalesce(PoliticalReport.report_date, PoliticalReport.filed_date)).label("latest_report_date"))
               .where(*active_reports()).group_by(PoliticalReport.person_id).subquery())
    query = (select(PoliticalPerson, reports.c.report_count, reports.c.latest_report_date)
             .outerjoin(reports, reports.c.person_id == PoliticalPerson.id)
             .order_by(PoliticalPerson.name, PoliticalPerson.source, PoliticalPerson.id))
    if source:
        query = query.where(PoliticalPerson.source == source)
    return {"items": [{**PoliticalPersonView.model_validate(person).model_dump(),
                       "report_count": count or 0, "latest_report_date": latest}
                      for person, count, latest in s.execute(query)]}


def report(s, report_id):
    row = s.get(PoliticalReport, report_id)
    if row is None:
        raise NotFoundError("political.report_not_found")
    fields = ("id", "source", "external_id", "filed_date", "report_date", "report_date_source", "url", "source_hash",
              "amendment_of_id", "amendment_status", "status", "parser_version",
              "page_count", "pages_done", "ocr_pages", "ocr_seconds")
    rows = s.scalars(select(PoliticalTransaction).where(PoliticalTransaction.report_id == row.id)
                     .order_by(PoliticalTransaction.page, PoliticalTransaction.row_number)).all()
    return {**{field: getattr(row, field) for field in fields},
            "file_url": f"/api/v1/political/reports/{row.id}/file" if row.source_hash else None,
            "person": PoliticalPersonView.model_validate(s.get(PoliticalPerson, row.person_id)),
            "rows": rows, "review_rows": [item for item in rows if item.confidence == "review"]}


def sources(s):
    reports = {row.source: row for row in s.execute(select(
        PoliticalReport.source, func.count().label("report_count"),
        func.count().filter(*active_reports()).label("active_report_count"),
        func.sum(PoliticalReport.page_count).label("page_count"),
        func.sum(PoliticalReport.pages_done).label("pages_done"),
        func.sum(PoliticalReport.ocr_pages).label("ocr_pages"),
        func.sum(PoliticalReport.ocr_seconds).label("ocr_seconds"),
        func.count().filter(or_(PoliticalReport.source_hash.is_(None),
            PoliticalReport.pages_done < PoliticalReport.page_count,
            PoliticalReport.status.in_(("discovered", "parsing", "review")))).label("pending_reports"),
    ).group_by(PoliticalReport.source))}
    trades = {row.source: row for row in s.execute(select(
        PoliticalReport.source, func.count().label("transaction_count"),
        func.count().filter(PoliticalTransaction.confidence == "review").label("review_count"),
    ).join(PoliticalTransaction, PoliticalTransaction.report_id == PoliticalReport.id)
        .where(*active_reports()).group_by(PoliticalReport.source))}
    reviews = dict(s.execute(select(PoliticalReport.source, func.count())
        .join(PoliticalTransaction, PoliticalTransaction.report_id == PoliticalReport.id)
        .where(PoliticalReport.status != "superseded", PoliticalTransaction.confidence == "review")
        .group_by(PoliticalReport.source)).all())
    items = []
    for source in ("house", "senate", "oge"):
        poll = s.get(SourcePoll, f"political_{source}")
        availability = (poll.last_result or {}).get("availability") if poll else None
        state = availability if availability in ("available", "unavailable") else "unknown"
        if poll and poll.last_error:
            state = "unavailable"
        checking = s.scalar(select(Job.id).where(Job.kind == "political_index", Job.status.in_(ACTIVE),
                            Job.target["source"].astext == source).limit(1)) is not None
        item = {"checking": checking, "source": source, "status": state, "last_error": poll.last_error if poll else None,
                "last_check_at": poll.last_polled_at if poll else None,
                "next_check_at": poll.next_poll_at if poll else None}
        for fields, row in ((("report_count", "active_report_count", "page_count", "pages_done", "ocr_pages",
                              "ocr_seconds", "pending_reports"), reports.get(source)),
                            (("transaction_count", "review_count"), trades.get(source))):
            item.update({field: getattr(row, field) or 0 if row else 0 for field in fields})
        item["review_count"] = reviews.get(source, 0)
        items.append(item)
    return {"items": items}
