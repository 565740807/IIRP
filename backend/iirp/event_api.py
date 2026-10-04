"""Event imports have separate preview, review and durable analysis commands."""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from iirp import event_service
from iirp.event_contracts import CustomEventsImport, EarningsEventsImport
from iirp.event_views import (
    EventAnalysisCreated,
    EventAnalysisOutput,
    EventConfirmOutput,
    EventErrorOutput,
    EventSetOutput,
    EventSetsOutput,
)
from iirp.import_limits import EVENT_REQUEST_BYTES, EVENT_TEXT_CHARACTERS, EVENT_TEXT_UTF8_BYTES

router = APIRouter(
    prefix="/api/v1/events",
    tags=["event research"],
    responses={code: {"model": EventErrorOutput} for code in (404, 409, 422)},
)


class EventCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EventPreviewInput(EventCommand):
    text: str = Field(min_length=2, max_length=EVENT_TEXT_CHARACTERS)
    set_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


class EventReviewInput(EventCommand):
    client_event_id: str = Field(min_length=1)
    selected: bool
    date_verified: bool = False
    time_verified: bool = False
    period_verified: bool = False
    note: str = ""


class EventConfirmInput(EventCommand):
    preview_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1, max_length=110)
    security_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)
    reviews: list[EventReviewInput]
    revision_note: str = ""
    analyze: bool = False


class EventAnalysisInput(EventCommand):
    request_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    cutoff_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    current_fiscal_year: int | None = Field(default=None, ge=1, le=9998)
    include_unverified: bool = False
    benchmark: str | None = None
    historical_years: int = Field(default=8, ge=1, le=9997)
    years: list[int] | None = None
    excluded_years: list[int] = Field(default_factory=list)
    common_years: bool = False
    date_window: Literal["before5", "day0", "after5", "through5"] = "after5"
    date_category: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def parameters(self):
        from iirp.benchmarks import validate_benchmark
        self.benchmark = validate_benchmark(self.benchmark)
        if self.years == [] or any(y < 1 or y > 9998 for y in [*(self.years or []), *self.excluded_years]):
            raise ValueError("请选择有效历史年份；空数组请省略")
        if self.years is not None and len(self.years) != len(set(self.years)):
            raise ValueError("指定历史年份不能重复，避免重复计算覆盖分母")
        if len(self.excluded_years) != len(set(self.excluded_years)):
            raise ValueError("排除历史年份不能重复")
        return self


class EventPreviewOutput(BaseModel):
    preview_id: str
    content_hash: str
    document: CustomEventsImport | EarningsEventsImport
    warnings: list[str]
    candidates: list[dict[str, Any]]
    set_id: str | None
    expected_version: int | None
    events: list[dict[str, Any]]


class EventPromptOutput(BaseModel):
    kind: Literal["custom", "earnings"]
    language: Literal["zh", "en"]
    schema_version: str
    prompt: str
    json_schema: dict[str, Any]
    input_schema_version: str
    input_example: dict[str, Any]
    input_json_schema: dict[str, Any]


class EventImportLimitsOutput(BaseModel):
    text_characters: int = EVENT_TEXT_CHARACTERS
    text_utf8_bytes: int = EVENT_TEXT_UTF8_BYTES
    request_bytes: int = EVENT_REQUEST_BYTES
    explanation: str = "字符按 Unicode 码点计数；UTF-8 与 JSON 包装/转义分别占字节。预览、修订预览、确认保存使用同一请求额度。"


@router.get("/limits", response_model=EventImportLimitsOutput)
def import_limits():
    return EventImportLimitsOutput()


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, [{"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]} for item in exc.errors()]) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/prompt", response_model=EventPromptOutput)
def prompt(kind: Literal["custom", "earnings"] = "custom", language: Literal["zh", "en"] = "zh"):
    return _call(event_service.event_prompt, kind, language)


@router.post("/preview", response_model=EventPreviewOutput)
def preview(body: EventPreviewInput):
    return _call(event_service.preview_import, body.model_dump(mode="json"))


@router.post("/confirm", response_model=EventConfirmOutput, response_model_exclude_unset=True)
def confirm(body: EventConfirmInput):
    return _call(event_service.confirm_import, body.model_dump(mode="json"))


@router.get("/sets", response_model=EventSetsOutput)
def sets(kind: Literal["custom", "earnings"] | None = None):
    return _call(event_service.list_sets, kind)


@router.get("/sets/{set_id}", response_model=EventSetOutput)
def detail(set_id: str, version: int | None = None):
    return _call(event_service.get_set, set_id, version)


@router.post("/sets/{set_id}/analyses", response_model=EventAnalysisCreated, status_code=202)
def create_analysis(set_id: str, body: EventAnalysisInput):
    return _call(event_service.create_analysis, set_id, body.model_dump(mode="json"))


@router.get("/analyses/{analysis_id}", response_model=EventAnalysisOutput, response_model_exclude_unset=True)
def analysis(analysis_id: str, result_id: str | None = None):
    return _call(event_service.get_analysis, analysis_id, result_id)


@router.get("/analyses/{analysis_id}/export")
def export(analysis_id: str, result_id: str, format: Literal["json", "csv"] = "csv"):
    output = _call(event_service.export_analysis, analysis_id, result_id, format)
    return Response(
        ("\ufeff" + output) if format == "csv" else output,
        media_type="text/csv; charset=utf-8" if format == "csv" else "application/json",
        headers={
            "Content-Disposition": f'attachment; filename="event-dates-{analysis_id}.{format}"'
        },
    )
