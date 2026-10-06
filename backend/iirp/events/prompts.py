"""Prompt templates the user copies to another AI platform (S3).

Each kind (earnings, custom) has a built-in Chinese and English default. A
saved template is user data and is kept until the user restores the default.
``{{tickers}}``, ``{{years}}`` and ``{{event}}`` are filled in by the page
before copying; anything else is the user's own text.
"""

from sqlalchemy import select

from iirp.db import session
from iirp.models import PromptTemplate, now

MAX_TEXT = 20000

_FORMAT = ('{"events":[{"ticker":"AAPL","date":"2025-10-30","session":"after_close",'
           '"name":"FY2025 Q4 earnings","fiscal_year":2025,"fiscal_quarter":4,"note":"…"}]}')
_CUSTOM_FORMAT = ('{"events":[{"ticker":"AAPL","date":"2025-06-09","session":"during",'
                  '"name":"WWDC 2025 keynote","note":"…"}]}')

DEFAULTS = {
    ("earnings", "zh"): f"""请查找以下美股公司最近 {{{{years}}}} 年（包括今年已经公布的）每一次季度财报的首次公布日期：{{{{tickers}}}}

只返回一个 JSON 对象，不要任何解释、Markdown 或代码块标记。格式：
{_FORMAT}

字段说明：
- ticker：股票代码，大写。
- date：财报新闻稿首次公开发布的日期（美东时间），YYYY-MM-DD。不是电话会议日期，也不是 10-Q/10-K 提交日期。
- session：发布时段。before_open = 美东 9:30 开盘前；during = 交易时段内；after_close = 16:00 收盘后；查不到就填 unknown，不要猜。
- name：简短名称，例如 "FY2025 Q4 earnings"。
- fiscal_year、fiscal_quarter：公司自己的财年和财季（整数，财季 1–4）。财年可能和自然年不同，以公司公告为准。
- note：可选，写来源或不确定的地方；没有就省略这个字段。

要求：每家公司每个财季一条，按日期从早到晚排列；查不到的财季直接省略，不要编造日期。""",
    ("earnings", "en"): f"""Find the first public release date of every quarterly earnings report in the last {{{{years}}}} years (including this year's releases so far) for these US-listed companies: {{{{tickers}}}}

Return exactly one JSON object and nothing else: no explanation, no Markdown, no code fences. Format:
{_FORMAT}

Fields:
- ticker: the stock symbol, upper case.
- date: the date the earnings press release was first made public (US Eastern), YYYY-MM-DD. Not the conference-call date and not the 10-Q/10-K filing date.
- session: before_open = before the 9:30 ET open; during = during regular trading hours; after_close = after the 16:00 ET close; use unknown if you cannot confirm it. Do not guess.
- name: a short name such as "FY2025 Q4 earnings".
- fiscal_year, fiscal_quarter: the company's own fiscal year and quarter (integers, quarter 1-4). The fiscal year may differ from the calendar year.
- note: optional; the source or any doubt. Omit the field if there is nothing to say.

One entry per company per fiscal quarter, sorted by date. Leave out a quarter you cannot find; never invent a date.""",
    ("custom", "zh"): f"""请查找最近 {{{{years}}}} 年里每一次“{{{{event}}}}”发生的日期，并为下列每只股票各写一条：{{{{tickers}}}}

只返回一个 JSON 对象，不要任何解释、Markdown 或代码块标记。格式：
{_CUSTOM_FORMAT}

字段说明：
- ticker：股票代码，大写。同一次事件要分析几只股票，就为每只股票各写一条（日期相同）。
- date：事件发生（或消息首次公开）的日期，按美东时间，YYYY-MM-DD。
- session：相对美股交易时段的时间。before_open = 美东 9:30 开盘前；during = 交易时段内；after_close = 16:00 收盘后；查不到就填 unknown，不要猜。
- name：简短名称，例如 "WWDC 2025 keynote"。
- note：可选，写来源或不确定的地方；没有就省略这个字段。

要求：按日期从早到晚排列；查不到的就省略，不要编造日期。""",
    ("custom", "en"): f"""Find the date of every occurrence of "{{{{event}}}}" in the last {{{{years}}}} years, and write one entry for each of these stocks: {{{{tickers}}}}

Return exactly one JSON object and nothing else: no explanation, no Markdown, no code fences. Format:
{_CUSTOM_FORMAT}

Fields:
- ticker: the stock symbol, upper case. Repeat each occurrence once per stock (same date).
- date: the date the event happened (or the news first became public), US Eastern, YYYY-MM-DD.
- session: relative to US trading hours. before_open = before the 9:30 ET open; during = during regular trading hours; after_close = after the 16:00 ET close; use unknown if you cannot confirm it. Do not guess.
- name: a short name such as "WWDC 2025 keynote".
- note: optional; the source or any doubt. Omit the field if there is nothing to say.

Sort by date. Leave out anything you cannot find; never invent a date.""",
}


def _check(kind, language):
    if (kind, language) not in DEFAULTS:
        raise LookupError("没有这个提示词模板")


def _view(kind, language, row):
    default = DEFAULTS[(kind, language)]
    return {"kind": kind, "language": language, "text": row.text if row else default,
            "is_default": row is None, "default_text": default,
            "updated_at": row.updated_at.isoformat() if row else None}


def get_prompt(kind, language):
    _check(kind, language)
    with session() as s:
        return _view(kind, language, s.get(PromptTemplate, (kind, language)))


def save_prompt(kind, language, text):
    _check(kind, language)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模板不能为空；要恢复默认请用“恢复默认”")
    if len(text) > MAX_TEXT:
        raise ValueError(f"模板不能超过 {MAX_TEXT} 个字符")
    with session() as s, s.begin():
        row = s.scalar(select(PromptTemplate).where(
            PromptTemplate.kind == kind, PromptTemplate.language == language).with_for_update())
        if row is None:
            row = PromptTemplate(kind=kind, language=language)
            s.add(row)
        row.text, row.updated_at = text, now()
        s.flush()
        return _view(kind, language, row)


def reset_prompt(kind, language):
    _check(kind, language)
    with session() as s, s.begin():
        row = s.get(PromptTemplate, (kind, language))
        if row is not None:
            s.delete(row)
    return _view(kind, language, None)
