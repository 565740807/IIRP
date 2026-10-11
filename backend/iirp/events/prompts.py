"""Prompt templates the user copies to another AI platform.

Each kind (earnings, custom) has a built-in Chinese and English default. A
saved template is user data and is kept until the user restores the default;
when the built-in default is newer than a saved template, the page says so.
``{{tickers}}``, ``{{years}}``, ``{{event}}``, ``{{context_before_days}}`` and
``{{context_after_days}}`` are filled in by the page before copying; anything
else is the user's own text.

The defaults work in two steps: the AI first lists its understanding of the
events and asks about anything unclear, without any JSON; only after the user
confirms does it return the JSON. They ask for an AI that can search the web
and for nothing that cannot be cited.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from iirp.db import session
from iirp.models import PromptTemplate, now

MAX_TEXT = 20000
# When the built-in defaults last changed; saved templates older than this
# were written against an earlier default.
DEFAULTS_CHANGED = datetime(2026, 10, 11, tzinfo=timezone.utc)

_CONTEXT = ('"context":[{"date":"2025-10-29","category":"macro","title":"…","summary":"…",'
            '"source":"…","url":"https://…","major":true}]')
_FORMAT = ('{"events":[{"ticker":"AAPL","date":"2025-10-30","session":"after_close",'
           '"name":"FY2025 Q4 earnings","fiscal_year":2025,"fiscal_quarter":4,'
           '"earnings":{"eps_actual":1.23,"eps_estimate":1.2,"revenue_actual":45678,'
           '"revenue_estimate":45000,"guidance":"maintained"},' + _CONTEXT + ',"note":"…"}]}')
_CUSTOM_FORMAT = ('{"events":[{"ticker":"AAPL","date":"2025-06-09","session":"during",'
                  '"name":"WWDC 2025 keynote",' + _CONTEXT + ',"note":"…"}]}')

_CONTEXT_ZH = """- context：可选，事件前 {{context_before_days}} 天到后 {{context_after_days}} 天内可能影响这只股票的信息，最多 8 条，按日期排列。每条：
  - date：YYYY-MM-DD；
  - category：company（公司新闻）/ earnings（财报细节）/ industry（行业）/ market（大盘）/ macro（宏观数据）/ policy（政策）/ geopolitical（地缘与国际）/ other（其他）；
  - title：一句话标题；summary：简短说明，不超过 300 字；
  - source：来源名称；url：可以打开的 http/https 链接；
  - major：可选，只有影响整个市场的重大事件（例如议息会议决议、超预期的 CPI、关税公告）才写 true，否则省略。
  找不到来源和链接的信息不要写。"""
_CONTEXT_EN = """- context: optional; up to 8 items within {{context_before_days}} days before to {{context_after_days}} days after the event that could have moved this stock, in date order. Each item:
  - date: YYYY-MM-DD
  - category: company / earnings / industry / market / macro / policy / geopolitical / other
  - title: one sentence; summary: a short explanation of at most 300 characters
  - source: the publisher or organization; url: a working http/https link
  - major: optional; true only for events that moved the whole market (for example a Fed decision, a CPI surprise, a tariff announcement); omit it otherwise.
  Leave out anything you cannot cite with a source and a link."""

DEFAULTS = {
    ("earnings", "zh"): f"""请帮我整理财报日期，用于研究股价对财报的反应。这项工作需要联网搜索：如果你不能上网，请直接告诉我并停止。只写你能找到出处的信息，不要编造日期、数字、链接或新闻。

公司：{{{{tickers}}}}
范围：最近 {{{{years}}}} 年每一次季度财报（包括今年已经公布的）
背景信息范围：每次财报前 {{{{context_before_days}}}} 天到后 {{{{context_after_days}}}} 天

第一步：先确认，先不要输出 JSON。
用一个简短的表列出你的理解：股票代码、财报新闻稿首次公布日期（美东）、名称、公布时段（开盘前 / 交易时段内 / 收盘后 / 不确定）、财年和财季。然后把所有不确定的地方逐条编号提问：指的是哪家公司或哪类股票、是哪几年、某个日期或时段是否准确、公司财年与自然季度怎样对应等。有疑问就问，不要猜。等我回复。

第二步：我确认之后，只返回一个 JSON 对象，不要任何解释、Markdown 或代码块标记。格式：
{_FORMAT}

字段说明：
- ticker：股票代码，大写。
- date：财报新闻稿首次公开发布的日期（美东时间），YYYY-MM-DD。不是电话会议日期，也不是 10-Q/10-K 提交日期。
- session：发布时段。before_open = 美东 9:30 开盘前；during = 交易时段内；after_close = 16:00 收盘后；查不到就填 unknown，不要猜。
- name：简短名称，例如 "FY2025 Q4 earnings"。
- fiscal_year、fiscal_quarter：公司自己的财年和财季（整数，财季 1–4）。财年可能和自然年不同，以公司公告为准。
- uncertain：可选，我确认之后这一条仍有疑问时写 true，并在 note 里说明原因；没有疑问就省略。
- note：可选，写来源或不确定的地方；没有就省略这个字段。
- earnings：可选，与市场预期的对比，每个数字都要有出处（实际值看公司公告，预期值写明数据来源）：
  - eps_actual、eps_estimate：每股收益（美元），实际值和预期值用同一口径（在 note 里写明是 GAAP 还是调整后）；
  - revenue_actual、revenue_estimate：营收，单位百万美元；
  - guidance：raised（上调）/ maintained（维持）/ lowered（下调）/ none（未给指引）。
  找不到出处的数字省略。
{_CONTEXT_ZH}

要求：每家公司每个财季一条，按日期从早到晚排列；查不到的财季直接省略，不要编造日期。""",
    ("earnings", "en"): f"""Help me collect earnings release dates for a study of how stocks react to earnings. This needs web search: if you cannot browse the web, say so and stop. Write only what you can cite from a source you actually found; never invent dates, numbers, links or news.

Companies: {{{{tickers}}}}
Period: every quarterly report in the last {{{{years}}}} years, including this year's releases so far
Background window: from {{{{context_before_days}}}} days before to {{{{context_after_days}}}} days after each release

Step 1: confirm first; do not output any JSON yet.
List your understanding in a short table: ticker, the date the earnings press release was first made public (US Eastern), name, session (before the open / during trading / after the close / unknown), fiscal year and quarter. Then ask numbered questions about everything you are unsure of: which company or share class is meant, which years, whether a date or session is right, how the fiscal year maps to calendar quarters. Ask; do not guess. Wait for my reply.

Step 2: only after I confirm, return exactly one JSON object and nothing else: no explanation, no Markdown, no code fences. Format:
{_FORMAT}

Fields:
- ticker: the stock symbol, upper case.
- date: the date the earnings press release was first made public (US Eastern), YYYY-MM-DD. Not the conference-call date and not the 10-Q/10-K filing date.
- session: before_open = before the 9:30 ET open; during = during regular trading hours; after_close = after the 16:00 ET close; use unknown if you cannot confirm it. Do not guess.
- name: a short name such as "FY2025 Q4 earnings".
- fiscal_year, fiscal_quarter: the company's own fiscal year and quarter (integers, quarter 1-4). The fiscal year may differ from the calendar year.
- uncertain: optional; true if anything about this entry is still in doubt after my confirmation, with the reason in note. Omit it otherwise.
- note: optional; the source or any doubt. Omit the field if there is nothing to say.
- earnings: optional; the comparison with expectations, every number from a source (actuals from the company's release, estimates from a named data provider):
  - eps_actual, eps_estimate: earnings per share in USD, both on the same basis (say in note whether GAAP or adjusted)
  - revenue_actual, revenue_estimate: revenue in USD millions
  - guidance: raised / maintained / lowered / none (none = no guidance given)
  Leave out any number you cannot source.
{_CONTEXT_EN}

One entry per company per fiscal quarter, sorted by date. Leave out a quarter you cannot find; never invent a date.""",
    ("custom", "zh"): f"""请帮我整理事件日期，用于研究股价对事件的反应。这项工作需要联网搜索：如果你不能上网，请直接告诉我并停止。只写你能找到出处的信息，不要编造日期、链接或新闻。

事件：“{{{{event}}}}”
股票：{{{{tickers}}}}
范围：最近 {{{{years}}}} 年里这个事件的每一次
背景信息范围：每次事件前 {{{{context_before_days}}}} 天到后 {{{{context_after_days}}}} 天

第一步：先确认，先不要输出 JSON。
用一个简短的表列出你的理解：每一次事件的日期（美东）、名称、相对美股交易时段的时间（开盘前 / 交易时段内 / 收盘后 / 不确定），以及要写哪几只股票。然后把所有不确定的地方逐条编号提问：这个事件具体指什么、包括哪几年、某个日期或时段是否准确等。有疑问就问，不要猜。等我回复。

第二步：我确认之后，只返回一个 JSON 对象，不要任何解释、Markdown 或代码块标记。格式：
{_CUSTOM_FORMAT}

字段说明：
- ticker：股票代码，大写。同一次事件要分析几只股票，就为每只股票各写一条（日期相同）。
- date：事件发生（或消息首次公开）的日期，按美东时间，YYYY-MM-DD。
- session：相对美股交易时段的时间。before_open = 美东 9:30 开盘前；during = 交易时段内；after_close = 16:00 收盘后；查不到就填 unknown，不要猜。
- name：简短名称，例如 "WWDC 2025 keynote"。
- uncertain：可选，我确认之后这一条仍有疑问时写 true，并在 note 里说明原因；没有疑问就省略。
- note：可选，写来源或不确定的地方；没有就省略这个字段。
{_CONTEXT_ZH}

要求：按日期从早到晚排列；查不到的就省略，不要编造日期。""",
    ("custom", "en"): f"""Help me collect event dates for a study of how stocks react to an event. This needs web search: if you cannot browse the web, say so and stop. Write only what you can cite from a source you actually found; never invent dates, links or news.

Event: "{{{{event}}}}"
Stocks: {{{{tickers}}}}
Period: every occurrence in the last {{{{years}}}} years
Background window: from {{{{context_before_days}}}} days before to {{{{context_after_days}}}} days after each occurrence

Step 1: confirm first; do not output any JSON yet.
List your understanding in a short table: the date of each occurrence (US Eastern), its name, its time relative to US trading hours (before the open / during trading / after the close / unknown), and which stocks you will write entries for. Then ask numbered questions about everything you are unsure of: what exactly the event is, which years it covers, whether a date or time is right. Ask; do not guess. Wait for my reply.

Step 2: only after I confirm, return exactly one JSON object and nothing else: no explanation, no Markdown, no code fences. Format:
{_CUSTOM_FORMAT}

Fields:
- ticker: the stock symbol, upper case. Repeat each occurrence once per stock (same date).
- date: the date the event happened (or the news first became public), US Eastern, YYYY-MM-DD.
- session: relative to US trading hours. before_open = before the 9:30 ET open; during = during regular trading hours; after_close = after the 16:00 ET close; use unknown if you cannot confirm it. Do not guess.
- name: a short name such as "WWDC 2025 keynote".
- uncertain: optional; true if anything about this entry is still in doubt after my confirmation, with the reason in note. Omit it otherwise.
- note: optional; the source or any doubt. Omit the field if there is nothing to say.
{_CONTEXT_EN}

Sort by date. Leave out anything you cannot find; never invent a date.""",
}


def _check(kind, language):
    if (kind, language) not in DEFAULTS:
        raise LookupError("没有这个提示词模板")


def _view(kind, language, row):
    default = DEFAULTS[(kind, language)]
    return {"kind": kind, "language": language, "text": row.text if row else default,
            "is_default": row is None, "default_text": default,
            "default_changed": row is not None and row.updated_at < DEFAULTS_CHANGED and row.text != default,
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
