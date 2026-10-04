"""Entity history snapshots with SQL paging and immutable observation rows.

The large ordered index stays in PostgreSQL. Python only receives the requested
page and small summary metadata; later source/amendment changes do not alter it.
"""

import uuid
from dataclasses import fields
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from sqlalchemy import (
    Date,
    Numeric,
    String,
    case,
    cast,
    column,
    func,
    insert,
    literal,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH, aggregate_order_by

from iirp.business_models import (
    FeedSession,
    Filing,
    FilingOwner,
    FilingVersion,
    Issuer,
    Owner,
    TransactionEvent,
)
from iirp.domain.ownership import OwnershipRow
from iirp.models import now
from iirp.ticker_identity import normalized_ticker


def _freeze_index(s, statement, filters, companies):
    """One MVCC statement stores both the index and its whole-range totals."""
    values = statement.cte("entity_rows")
    valid = values.c.status == "CURRENT"
    keys = [values.c.table, values.c.security_title, values.c.currency, values.c.code]
    owner_ids = func.jsonb_array_elements_text(values.c.owner_ids).table_valued("value").render_derived().lateral("owner_ids")
    relationship = FilingOwner.relationship
    person = (relationship["is_director"].astext.in_(["1", "true"]) | relationship["is_officer"].astext.in_(["1", "true"]))
    institution = relationship["entity_type"].astext == "institution"
    # The transaction stays a single joint filing row, but a person's page
    # counts only that subject. Company history counts all reported subjects.
    owner_scope = owner_ids.c.value == filters["id"] if filters["kind"] in {"owner", "person"} else true()
    owner_counts = (select(*keys,
        func.count(func.distinct(owner_ids.c.value)).label("owner_count"),
        func.count(func.distinct(case((person, owner_ids.c.value)))).label("person_count"),
        func.count(func.distinct(case((institution, owner_ids.c.value)))).label("institution_count"))
        .select_from(values).join(owner_ids, owner_scope).outerjoin(FilingOwner, (FilingOwner.version_id == values.c.version_id) & (FilingOwner.owner_id == owner_ids.c.value)).group_by(*keys).subquery("owner_counts"))
    groups = (
        select(
            *keys,
            func.min(values.c.kind).label("kind"),
            func.count().label("rows"),
            func.count().filter(~valid).label("review_rows"),
            func.count().filter(~valid & (values.c.shares.is_not(None) | values.c.price.is_not(None))).label("reported_value_review_rows"),
            func.count().filter(~valid & values.c.shares.is_(None) & values.c.price.is_(None)).label("missing_source_value_rows"),
            func.count().filter(valid & values.c.shares.is_not(None)).label("known_shares_rows"),
            func.count().filter(valid & values.c.amount.is_not(None)).label("known_price_rows"),
            func.count().filter(valid & values.c.amount.is_(None)).label("missing_price_rows"),
            cast(func.sum(values.c.shares).filter(valid), String).label("shares"),
            cast(func.sum(values.c.amount).filter(valid), String).label("known_amount"),
        )
        .group_by(*keys)
        .subquery("entity_summary")
    )
    summary = (
        select(
            func.coalesce(
                func.jsonb_agg(
                    func.jsonb_build_object(
                        *[part for key in groups.c.keys() for part in (key, groups.c[key])]
                        , "owner_count", owner_counts.c.owner_count,
                        "person_count", owner_counts.c.person_count,
                        "institution_count", owner_counts.c.institution_count,
                        "unknown_owner_count", owner_counts.c.owner_count - owner_counts.c.person_count - owner_counts.c.institution_count
                    )
                ),
                literal([], JSONB),
            )
        )
        .select_from(groups).outerjoin(owner_counts, (groups.c.table.is_not_distinct_from(owner_counts.c.table)) & (groups.c.security_title.is_not_distinct_from(owner_counts.c.security_title)) & (groups.c.currency.is_not_distinct_from(owner_counts.c.currency)) & (groups.c.code.is_not_distinct_from(owner_counts.c.code)))
        .scalar_subquery()
    )
    refs = func.coalesce(
        func.jsonb_agg(
            aggregate_order_by(
                func.jsonb_build_array(
                    values.c.id,
                    values.c.version_id,
                    values.c.row_key,
                    values.c.status,
                    values.c.replaces_id,
                ),
                values.c.sort_date.desc().nulls_last(),
                values.c.id.desc(),
            )
        ),
        literal([], JSONB),
    )
    identifier = str(uuid.uuid4())
    metadata = literal(filters, JSONB).op("||")(
        func.jsonb_build_object("total", func.count(), "summary", summary, "companies", companies,
                               "transaction_start", func.min(values.c.transaction_date), "transaction_end", func.max(values.c.transaction_date),
                               "filings", func.count(func.distinct(values.c.accession)))
    )
    # The first immutable index can cover a large, explicitly requested scope.
    # Give this one statement a bounded construction budget; subsequent page
    # reads retain the connection's short normal timeout.
    previous_timeout = s.scalar(select(func.current_setting("statement_timeout")))
    s.execute(select(func.set_config("statement_timeout", "20000", True)))
    s.execute(
        insert(FeedSession).from_select(
            [FeedSession.id, FeedSession.revision_ids, FeedSession.filters, FeedSession.expires_at],
            select(
                literal(identifier), refs, metadata, literal(now() + timedelta(hours=12))
            ).select_from(values),
        )
    )
    s.execute(select(func.set_config("statement_timeout", previous_timeout, True)))
    return identifier


def _company_options(kind, identifier, visible):
    """All locally observed companies; freeze with the page index in one statement."""
    statement = (
        select(TransactionEvent.issuer_id, TransactionEvent.version_id)
        .join(Filing, Filing.accession == TransactionEvent.accession)
        .where(TransactionEvent.status.in_(visible), Filing.visible.is_(True))
    )
    statement = statement.where(
        TransactionEvent.issuer_id == identifier
        if kind in {"company", "issuer"}
        else TransactionEvent.owner_ids.contains([identifier])
    )
    latest = (
        statement.distinct(TransactionEvent.issuer_id)
        .order_by(
            TransactionEvent.issuer_id,
            TransactionEvent.accepted_at.desc().nulls_last(),
            TransactionEvent.id.desc(),
        )
        .subquery("entity_company_versions")
    )
    # Extract source metadata only once per company, after choosing its version.
    companies = (
        select(
            latest.c.issuer_id,
            FilingVersion.data["issuer_name"].astext.label("name"),
            FilingVersion.data["issuer_ticker"].astext.label("ticker"),
        )
        .select_from(latest)
        .join(FilingVersion, FilingVersion.id == latest.c.version_id)
        .subquery("entity_companies")
    )
    return (
        select(
            func.coalesce(
                func.jsonb_agg(
                    aggregate_order_by(
                        func.jsonb_build_object(
                            "issuer_id",
                            companies.c.issuer_id,
                            "name",
                            companies.c.name,
                            "ticker",
                            func.nullif(func.upper(func.trim(companies.c.ticker)), "NONE"),
                            "issuer_ticker_raw",
                            companies.c.ticker,
                        ),
                        companies.c.issuer_id,
                    )
                ),
                literal([], JSONB),
            )
        )
        .select_from(companies)
        .scalar_subquery()
    )


def _immutable_rows(s, references):
    from iirp.sec_facts import _compact_row, _date, _event_view, _instant, _row_data

    if not references:
        return []
    version_ids = list({reference[1] for reference in references})
    versions = {}
    for version in s.execute(
        select(
            FilingVersion.id,
            FilingVersion.accession,
            FilingVersion.data["rows"].label("rows"),
            FilingVersion.data["owners"].label("owners"),
            FilingVersion.data["issuer_cik"].astext.label("issuer_cik"),
            FilingVersion.data["issuer_name"].astext.label("issuer_name"),
            FilingVersion.data["issuer_ticker"].astext.label("issuer_ticker"),
            FilingVersion.data["form_type"].astext.label("form_type"),
            FilingVersion.data["raw_10b5_1_flag"].label("raw_10b5_1_flag"),
            FilingVersion.data["source_metadata"]["accepted_at"].astext.label("accepted_at"),
        ).where(FilingVersion.id.in_(version_ids))
    ).mappings():
        versions[version["id"]] = dict(version)
    result = []
    for identifier, version_id, row_key, status, replaces_id in references:
        version = versions.get(version_id)
        if not version:
            raise ValueError("阅读版本引用的原始观察缺失，请核对来源存储。")
        source_row = next(
            (
                row
                for row in version["rows"] or []
                if f"{row.get('table')}:{row.get('row_kind')}:{row.get('source_row_index')}"
                == row_key
            ),
            None,
        )
        if source_row is None:
            raise ValueError("阅读版本缺少不可变交易行，请重新解析该份原文。")
        row = OwnershipRow(
            **{field.name: source_row.get(field.name) for field in fields(OwnershipRow)}
        )
        owners = [SimpleNamespace(**owner) for owner in version["owners"] or []]
        observation = SimpleNamespace(
            **{
                key: version[key]
                for key in ("issuer_ticker", "issuer_name", "form_type", "raw_10b5_1_flag")
            },
            footnotes=[],
        )
        immutable = SimpleNamespace(
            id=identifier,
            issuer_id=version["issuer_cik"],
            accession=version["accession"],
            version_id=version_id,
            transaction_date=_date(source_row.get("transaction_date")),
            accepted_at=_instant(version["accepted_at"]),
            owner_ids=sorted({owner.cik for owner in owners if owner.cik}),
            status=status,
            replaces_id=replaces_id,
            data=_row_data(row, observation, owners),
        )
        result.append(
            {
                **_compact_row(_event_view(immutable, snapshot_status=status, compact=True)),
                "issuer_name": version["issuer_name"],
            }
        )
    return result


def read_entity_history(
    s,
    kind,
    identifier,
    start=None,
    end=None,
    recent_count=None,
    date_basis="transaction_date",
    cursor="",
    limit=20,
    session_id="",
    issuer_id="",
    action="all",
):
    from iirp.sec_facts import ET, VISIBLE, _canonical_kind, _cik, _date, _json, _offset, _summary

    action = _canonical_kind(action)

    date_basis = {"transaction": "transaction_date", "accepted": "accepted_at"}.get(
        date_basis, date_basis
    )
    recent_count = None if recent_count == 0 else recent_count
    if cursor and ":" in cursor:
        embedded, cursor = cursor.split(":", 1)
        if session_id and session_id != embedded:
            raise ValueError("分页游标与阅读快照不一致。")
        session_id = embedded
    if kind not in {"company", "issuer", "owner", "person"}:
        raise ValueError("请选择公司或申报主体。")
    identifier = _cik(identifier)
    issuer_id = _cik(issuer_id) if issuer_id else None
    if not 1 <= limit <= 100 or date_basis not in {
        "transaction_date",
        "accepted_at",
        "accepted_date",
    }:
        raise ValueError("历史页大小或时间口径无效。")
    start, end = _date(start), _date(end)
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    if recent_count is not None and (type(recent_count) is not int or recent_count < 1):
        raise ValueError("最近条数必须是正整数。")
    filters = {
        "purpose": "entity",
        "kind": kind,
        "id": identifier,
        "start": _json(start),
        "end": _json(end),
        "recent_count": recent_count,
        "date_basis": date_basis,
        "issuer_id": issuer_id,
        "action": action,
    }
    offset = _offset(cursor)
    if not session_id:
        entity = s.get(Issuer if kind in {"company", "issuer"} else Owner, identifier)
        if not entity:
            return {
                "items": [],
                "data": {
                    "id": identifier,
                    "kind": kind,
                    "items": [],
                    "entity": {"id": identifier, "kind": kind, "name": identifier},
                    "status": "NOT_FETCHED",
                    "companies": [],
                    "date_basis": date_basis,
                    "coverage": {
                        "status": "NOT_FETCHED",
                        "start_date": _json(start),
                        "end_date": _json(end),
                        "requested_count": recent_count,
                        "observed_count": 0,
                        "complete": False,
                        "message": "本地尚无该主体的解析历史。",
                    },
                    "message": "本地尚无该主体的解析历史。",
                },
            }
        ticker = (
            s.scalar(
                select(FilingVersion.data["issuer_ticker"].astext).where(
                    FilingVersion.id == select(Filing.current_version)
                    .where(Filing.issuer_id == identifier, Filing.visible.is_(True))
                    .order_by(Filing.accepted_at.desc().nulls_last(), Filing.accession.desc())
                    .limit(1)
                    .scalar_subquery()
                )
            )
            if kind in {"company", "issuer"}
            else None
        )
        # A transaction dated after its own SEC acceptance date is kept and
        # flagged, but never orders the history or widens its date range.
        anomaly = TransactionEvent.transaction_date > cast(
            func.timezone("America/New_York", TransactionEvent.accepted_at), Date
        )
        transaction_date = case((anomaly, None), else_=TransactionEvent.transaction_date)
        sort_date = (
            transaction_date
            if date_basis == "transaction_date"
            else TransactionEvent.accepted_at
        )
        statement = (
            select(
                TransactionEvent.id,
                TransactionEvent.version_id,
                TransactionEvent.row_key,
                TransactionEvent.status,
                TransactionEvent.replaces_id,
                sort_date.label("sort_date"),
                TransactionEvent.data,
                TransactionEvent.owner_ids,
                TransactionEvent.accession,
                transaction_date.label("transaction_date"),
            )
            .select_from(TransactionEvent)
            .join(Filing, Filing.accession == TransactionEvent.accession)
        )
        statement = statement.where(TransactionEvent.status.in_(VISIBLE), Filing.visible.is_(True))
        statement = (
            statement.where(TransactionEvent.issuer_id == identifier)
            if kind in {"company", "issuer"}
            else statement.where(TransactionEvent.owner_ids.contains([identifier]))
        )
        if issuer_id:
            statement = statement.where(TransactionEvent.issuer_id == issuer_id)
        table, code = TransactionEvent.data["table"].astext, TransactionEvent.data["code"].astext
        if action == "buy":
            statement = statement.where(table == "I", code == "P")
        elif action == "sell":
            statement = statement.where(table == "I", code == "S")
        elif action == "focus":
            statement = statement.where((table == "II") | code.in_(["P", "S"]))
        elif action == "derivative":
            statement = statement.where(table == "II")
        elif action == "other":
            statement = statement.where((table != "II") | table.is_(None), code.not_in(["P", "S"]) | code.is_(None))
        if start:
            statement = statement.where(
                sort_date
                >= (
                    start
                    if date_basis == "transaction_date"
                    else datetime.combine(start, time.min, tzinfo=ET)
                )
            )
        if end:
            statement = (
                statement.where(sort_date <= end)
                if date_basis == "transaction_date"
                else statement.where(
                    sort_date < datetime.combine(end + timedelta(days=1), time.min, tzinfo=ET)
                )
            )
        if recent_count:
            statement = statement.order_by(
                sort_date.desc().nulls_last(), TransactionEvent.id.desc()
            ).limit(recent_count)
        selected = statement.cte("selected_facts")
        # jsonb_to_record decompresses each source data value once. Large raw XML
        # is never projected into the final index, metadata or Python objects.
        record = (
            func.jsonb_to_record(selected.c.data)
            .table_valued(
                *[
                    column(key, String)
                    for key in (
                        "table",
                        "security_title",
                        "currency",
                        "code",
                        "kind",
                        "shares",
                        "price_per_share",
                    )
                ]
            )
            .render_derived(with_types=True)
            .lateral("fields")
        )
        shares, price = cast(record.c.shares, Numeric), cast(record.c.price_per_share, Numeric)
        statement = (
            select(
                *[
                    selected.c[key]
                    for key in ("id", "version_id", "row_key", "status", "replaces_id", "sort_date", "owner_ids", "accession", "transaction_date")
                ],
                record.c.table,
                record.c.security_title,
                record.c.currency,
                record.c.code,
                record.c.kind,
                shares.label("shares"),
                price.label("price"),
                (shares * price).label("amount"),
            )
            .select_from(selected)
            .join(record, true())
        )
        session_id = _freeze_index(
            s,
            statement,
            {
                **filters,
                "schema": "entity-index-v1",
                "summary_owner_scope": "focused_subject" if kind in {"owner", "person"} else "all_filing_owners",
                "as_of": now().isoformat(),
                "entity": {"id": identifier, "name": entity.name, "kind": kind, "ticker": normalized_ticker(ticker), "issuer_ticker_raw": ticker},
            },
            _company_options(kind, identifier, VISIBLE),
        )
    saved = s.execute(
        select(FeedSession.filters, FeedSession.expires_at).where(FeedSession.id == session_id)
    ).first()
    if not saved or saved.expires_at < now() or saved.filters.get("purpose") != "entity":
        raise ValueError("阅读快照不存在或已过期，请刷新后重新打开。")
    metadata = saved.filters
    if any(metadata.get(key, "all" if key == "action" else None) != value for key, value in filters.items()):
        raise ValueError("历史条件已经变化，请创建新的阅读快照。")
    if metadata.get("schema") == "entity-index-v1":
        # SQL slices before returning data: never transfer the full ID index.
        references = s.scalar(
            select(
                func.jsonb_path_query_array(
                    FeedSession.revision_ids, cast(f"$[{offset} to {offset + limit - 1}]", JSONPATH)
                )
            ).where(FeedSession.id == session_id)
        )
        items = _immutable_rows(s, references)
        total, summary, entity = metadata["total"], metadata["summary"], metadata["entity"]
    else:
        # Backwards-compatible reading of pre-upgrade snapshots until expiry.
        items = metadata["rows"][offset : offset + limit]
        total, summary = len(metadata["rows"]), _summary(metadata["rows"])
        old = s.get(Issuer if kind in {"company", "issuer"} else Owner, identifier)
        entity = {
            "id": identifier,
            "name": old.name if old else identifier,
            "kind": kind,
            "ticker": None,
        }
    companies = []
    for company in metadata.get("companies", []):
        item = dict(company)
        raw = item.pop("issuer_ticker_raw", item.get("ticker"))
        item["ticker"] = normalized_ticker(item.get("ticker"))
        if item["ticker"] is None and raw:
            item["issuer_ticker_raw"] = raw
        companies.append(item)
    if entity.get("ticker") is not None:
        entity = {key: value for key, value in entity.items() if key != "issuer_ticker_raw"}
    return {
        "items": items,
        "data": {
            "id": identifier,
            "kind": kind,
            "name": entity["name"],
            "entity": entity,
            "items": items,
            "session_id": session_id,
            "as_of": metadata["as_of"],
            "total": total,
            "next_cursor": f"{session_id}:{offset + limit}" if offset + limit < total else None,
            "summary": summary,
            "summary_owner_scope": metadata.get("summary_owner_scope", "legacy_joint_subjects" if kind in {"owner", "person"} else "all_filing_owners"),
            "summary_note": "此保留阅读的人数含联合申报主体；重新应用本地筛选可查看仅此主体的摘要。" if kind in {"owner", "person"} and not metadata.get("summary_owner_scope") else None,
            "transaction_start": metadata.get("transaction_start"),
            "transaction_end": metadata.get("transaction_end"),
            "filings": metadata.get("filings"),
            "date_basis": date_basis,
            "companies": companies,
            "coverage": {
                "status": "LOCAL_OBSERVATIONS",
                "start_date": metadata.get("start"),
                "end_date": metadata.get("end"),
                "requested_count": recent_count,
                "observed_count": total,
                "complete": False,
                "message": "已有记录可查看；全市场索引、原文和修订仍需按目标范围核对。",
            },
        },
    }
