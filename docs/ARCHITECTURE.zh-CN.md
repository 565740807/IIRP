# 架构说明

本文简述 IIRP 的模块、数据流、任务模型和存储，帮助读者快速定位代码。功能与使用见 [README](../README.zh-CN.md)，接口以 [openapi.json](openapi.json) 为准。

## 组成

```
浏览器（React + TypeScript，frontend/src）
   │  同源 JSON，/api/v1
   ▼
web：FastAPI（iirp.api.app:app）  ── 只读本地数据；写操作只创建持久任务
   │
   ▼
PostgreSQL 18  ◄── worker：python -m iirp.jobs.worker（领取任务、调用外部来源、提交事实）
   │                         │
   │                         ▼
   │                  SEC EDGAR / Yahoo（yfinance）
   ▼
runtime/ 卷：来源原文（按内容哈希存放）、备份、维护日志
```

Compose 启动三个服务：`postgres`、`web`（启动时先执行 `alembic upgrade head`）、`worker`。web 和 worker 使用同一个镜像。API 从不在请求中等待外部来源；浏览器关闭后 worker 继续工作。

## 目录

```
backend/iirp/    后端（Python 包，按领域分包，见下）
frontend/src/    前端：pages/ 页面、components/ 共享组件、locales/ 翻译、generated/ 接口类型
migrations/      Alembic 迁移：0001_baseline（2026-10 合并的结构基线，SQL 在同名 .sql）及之后的新迁移
config/          采集、分析默认值与来源契约
deploy/          Dockerfile、compose.yaml、环境变量模板（./iirp start 首次运行时据此生成 deploy/.env）
scripts/         ./iirp 的实现（cli.py）、备份与恢复验证、OpenAPI 导出与检查、读取基准
tests/           pytest，目录与后端包一一对应
docs/            本文、运行手册、openapi.json、README 截图（images/）；dev/ 是开发计划与历史记录
```

## 后端模块（`backend/iirp`）

依赖方向自上而下；`models`、`db`、`config`、`messages` 被所有包使用，下层不导入上层。

```
                         api（HTTP 路由与响应模型）
                          │
          ┌───────────────┼──────────────────────┐
          ▼               ▼                      ▼
   jobs（任务、批次、    events（财报/事件      storage（来源文件、
   规划、worker）        AI-JSON 与分析）       备份、维护、盘点）
          │               │
   ┌──────┼──────────┐    │
   ▼      ▼          ▼    ▼
  sec   insider    analysis（月度、区间、事件窗口统计）
   │   （交易事实、     │
   │    信息流）        ▼
   └──────┬────────► market（Yahoo 适配、24 小时日线缓存、报价）
          ▼
   models · db · config · messages
```

| 包 | 一句话说明 |
|---|---|
| `api/` | FastAPI 应用（`app.py`）、各领域路由（`research.py`、`events.py`）、本地只读投影（`reads.py`）和请求/响应模型（`schemas.py`，Insider 读取的响应模型在 `insider_schemas.py`）；Insider 查询、取价与前后 n 日在 `insider.py`，系统健康检查在 `health.py`；`docs/openapi.json` 由它导出。 |
| `jobs/` | 持久任务与租约（`queue.py`、`ownership.py`）、用户需求批次（`batches.py`、`batch_views.py`）、规划（`planner.py`）、执行（`worker.py`、`handlers.py`、`operations.py`、`operation_pool.py`）、自动更新与定时（`auto_update.py`、`schedule.py`）。 |
| `sec/` | SEC EDGAR：URL 校验与解析（`parse.py`、`ownership.py`）、限速下载（`fetch.py`）、历史范围规划（`planning.py`）、最新申报轮询（`poll.py`）、按 CIK 获取一家公司或一个人的申报（`entity.py`）、User-Agent 用的联系信息（`contact.py`）。 |
| `insider/` | 把申报写成按行的交易事实（`facts.py`），信息流的修订、水位线与阅读会话（`views.py`、`feed_index.py`、`feed.py`、`feed_updates.py`），公司/人员历史与交易详情（`entities.py`、`transactions.py`、`records.py`）、Insider 查询（`lookup.py`）。 |
| `market/` | Yahoo 适配器与日线校验（`yahoo.py`）、24 小时日线缓存（`cache.py`）、首页报价（`quotes.py`）、本地行情读取（`reads.py`）。 |
| `analysis/` | 所有金融计算：板块涨跌、周期锚点、热力图权重与周/月 K 聚合（`sectors.py`）；交易日历、月度与区间研究（`research.py`：每年一根“首个交易日开盘 → 最后交易日收盘”的 K 线、完整过去年份的统计、上涨比例的 Wilson 区间、同日期基准与超额）、分布统计（`distributions.py`）、事件窗口（`event_windows.py`）、交易前后窗口（单笔 `transaction_windows.py`，列表 `insider_windows.py`）；分析请求、结果复用与过期后重新获取。前端不重复实现。 |
| `events/` | 财报与自定义事件：提示词模板（`prompts.py`）、简短 JSON 校验（`input.py`）、事件集与分析（`service.py`）。 |
| `storage/` | 按内容哈希保存来源文件（`objects.py`）、维护与备份调度（`maintenance.py`）、存储盘点与系统状态。 |
| `models/` | 全部表定义，按领域分文件（`insider.py`、`market.py`、`jobs.py`、`analysis.py`、`sources.py`）；统一从 `iirp.models` 导入。 |
| `messages.py` | 返回给用户的文字是“消息代码 + 参数”，见下文“界面文字”。 |

迁移在 `migrations/`（Alembic）。采集与保留的默认值在 `config/collection-defaults.toml`，刷新间隔在 `config/refresh.toml`，分析的 n 默认值在 `config/analysis-defaults.toml`，板块与 ETF 的对应和热力图参数在 `config/sector-map.toml`，来源契约在 `config/provider-contracts.json`。

## 前端（`frontend/src`）

页面在 `pages/`，共享组件在 `components/`。已改造的部分用 Tailwind 和 [shadcn/ui](https://ui.shadcn.com/)（`components/ui/`，Radix 组件）：外壳在 `components/shell/`（侧栏、顶部栏、搜索、任务入口、语言切换、系统状态），首页在 `components/home/`（行情条 `MarketStrip`、Insider 瀑布流 `InsiderStream` 与卡片 `FeedCard`），Insider 查询 `pages/Insiders.tsx`、公司/人员页 `pages/Entity.tsx`、交易详情 `pages/Transaction.tsx`、数据与任务 `pages/Data.tsx`，它们共用 `components/insider/`（K 线 `PriceChart`、交易表 `TradeTable`、前后 n 日单元格、范围与 n 控件）。分析中心的外壳与月度/区间结果在 `components/analysis/`（四个页签、可收起的条件栏、逐只股票的分步进度、结论卡、年份 × 月份热力表、每年一根 K 线、排名与统计表、逐年明细、计算方法），月度与区间页是 `pages/Seasonal.tsx`，请求、轮询和 24 小时过期后自动重取在 `lib/analysis.ts`。财报与事件页是 `pages/Events.tsx`，组件在 `components/events/`（左侧输入流程 `EventInputs`：提示词、粘贴 JSON 与预览改时段、n 与基准、已保存事件集；结果 `EventResults`；图表 `EventCharts`：一个事件一根反应日 K 线、R−n…R+n 平均路径、单个事件日 K 线；表格 `EventTables`），请求与结论句在 `lib/events.ts`。指数详情页 `pages/Market.tsx` 显示保存的报价与最近 6 个月日 K 线。板块页是 `pages/Sectors.tsx`，组件在 `components/sectors/`（TanStack Table 数字表、ECharts treemap 热力图、`echarts.connect` 同步的纵向 K 线，只渲染进入可视区域的图），首页板块条是 `components/home/SectorStrip.tsx`，请求与 URL 状态在 `lib/sectors.ts`、`lib/sectorState.ts`。旧的 `legacy.css` 已全部删除，样式只用 Tailwind 与 shadcn/ui。

- **请求**：新代码用 [openapi-fetch](https://openapi-ts.dev/openapi-fetch/)（`lib/api-client.ts`），类型由 `docs/openapi.json` 生成到 `generated/api.ts`（`npm run generate:api`，`npm run check:api` 校验一致）；服务器状态用 TanStack Query。前端只展示后端计算好的结果，不重复实现金融计算。
- **配色**：令牌在 `index.css`。涨/买为蓝（`--up`），跌/卖为橙（`--down`），一律带 + / − 号；主操作色是近黑的墨色，与蓝、橙都能区分；数据延迟用紫灰（`--warn`），不与“跌”混淆。文字与背景的组合在 Chrome 中实测对比度均 ≥ 4.5:1，并在 Chrome 的绿色盲（deuteranopia）模拟下检查过。
- **动画**：[Motion](https://motion.dev/) 负责新卡片落入、展开收起，[NumberFlow](https://number-flow.barvian.me/) 负责行情数字滚动（变化时背景闪蓝或橙约 1 秒）；首次加载用骨架；后台刷新时顶部栏刷新图标转动并显示细进度线；数据延迟的标记缓慢呼吸。时长约 0.15–0.3 秒，系统开启“减少动态效果”时不做位移动画。
- **长列表与表格**：瀑布流用 [TanStack Virtual](https://tanstack.com/virtual) 按窗口虚拟化；交易表和任务表用 [TanStack Table](https://tanstack.com/table)；K 线用 ECharts。

### 首页刷新

间隔只写在 `config/refresh.toml`：SEC 轮询节奏、行情的开盘/盘前盘后/休市规则，以及浏览器端的间隔（重读本地行情、检查新申报、询问服务器是否该更新、重读 Insider 总览）。浏览器端间隔随 `/api/v1/home` 下发。外部请求只由 worker 发出；页面打开、回到前台和可见期间每隔一段时间调用 `POST /api/v1/freshness/ensure`，服务器用咨询锁、“60 秒内已请求”和每个品种的 `next_refresh_at` 合并请求，所以多个标签页不会让 SEC 或 Yahoo 请求成倍增加；标签页隐藏时不轮询。行情是否“交易中 / 已休市 / 延迟”在读取时按当时时间判断（`api/reads.py`），延迟会写明原因（超时未更新、来源报价滞后、最近一次更新失败、收盘价尚未取回）。

### 板块

- **映射**：`config/sector-map.toml` 列出 11 个大板块和代表 ETF（SPDR 板块 ETF，代表标普 500 的对应板块，不覆盖全部美股）；结构上每个板块可再列行业组（主 ETF、备选 ETF、覆盖程度 complete/partial/reference/pending），目前只有大板块。
- **数据**（`analysis/sectors.py`）：日线用 24 小时缓存，`POST /api/v1/sectors/prices` 为缓存不够的 ETF 建一个批次，每只 ETF 一次取“今年 + 过去 8 个完整年”（缓存另加 1 个月余量），同一天到下一次收盘前重复请求返回同一批次；月度/区间分析打开这些 ETF 时直接复用。上市晚于窗口起点的 ETF（如 XLC）按实际年份计数，不补造历史。报价走共享的批量股票报价（`POST /api/v1/insider/quotes`，一次请求全部代码），节奏同首页指数（开盘 60 秒、盘前盘后 5 分钟、休市不刷新）。`GET /api/v1/sectors` 与 `GET /api/v1/sectors/candles` 只读本地缓存，切换视图、周期、范围和排序不会触发外部请求。
- **口径**：价格收益，仅拆股调整。1 周/1 个月/3 个月的终点是缓存中最近完成交易日 L 的收盘，起点是“L 往前 7 天/1 个月/3 个月那天或之前最近交易日”的收盘（3/31 往前 1 个月为 2/28 或 2/29）；年初至今的起点是上一年最后交易日收盘。今日：常规时段用最新常规时段报价对比前一交易日收盘（标“盘中 · 更新时间”）；其余时间用 L 收盘对比前一交易日收盘。热力图面积权重 = 0.2 + 0.8 × (r − r_min) / max(r_max − r_min, s)，s 按周期取 1%/2%/4%/8%/8%。周 K 按周一至周五、月 K 按自然月由日线聚合（开 = 首日开盘、收 = 末日收盘、高低取极值）。

### 瀑布流

信息流按“公司 × SEC 接受日（美东）”分组，每组里每位申报人一行：谁（职务）、交易代码对应的 SEC 说法、股数、金额（买入为 +、卖出为 −）、交易日；卡片右上角是披露时间。列表在服务器的阅读会话（水位线，见下文“信息流的阅读会话”）上工作：读者停在顶部时，新申报读取两条水位线之间的增量后立即落入；读者往下翻时列表不动，顶部出现“↑ N new trades”，点击后先回到顶部再落入；接近底部时用同一会话的游标加载更早的组。列表操作是纯函数（`lib/feedStream.ts`），有单元测试。

### Insider 查询与前后 n 日

- **查询**（`insider/lookup.py`）：先查本地（`issuer.ticker` 精确匹配，或名字包含每个词）；本地没有时由 worker 的 `sec_identity` 任务去 SEC 查：像 ticker 的输入查 `company_tickers_exchange.json`，名字查 EDGAR 公司与人员搜索（`efts.sec.gov/LATEST/search-index?keysTyped=…`，即 sec.gov 搜索框背后的公开接口），同一查询每天只请求一次。选中一项后建 `sec_entity` 批次：`sec_discover`（mode=entity）读该 CIK 的 `data.sec.gov/submissions` 文件（Form 3/4/5 同时列在发行人和每位申报人名下），只为范围内的申报建 `sec_document` 任务。全部走现有 SEC 限速（每秒 ≤ 2 次）和 User-Agent。
- **前后 n 日**（`analysis/insider_windows.py`）：以交易日 t（非交易日顺延）收盘为基准，前 n 日 = C(t)/C(t−n) − 1，后 n 日 = C(t+n)/C(t) − 1；尚未到来的交易日留空并给出预计日期，当天未收盘标“盘中”。价格来自申报里写的 ticker 的 24 小时缓存；该证券未经 SEC ticker 列表确认属于该发行人时照常计算并标“待核对”。`GET /insider/windows` 只读缓存；缺价时页面调用一次 `POST /insider/prices`，每只股票一次请求取“默认 6 个月与最早 t−n 的较早者 − 1 个月余量”到当天，所以随后打开公司页不再取价。n 默认值在 `config/analysis-defaults.toml`，页面上切换并记住。
- **总览**（`insider/overview.py`，`/insider` 页）：多人买入/卖出、最大买入/卖出、高管买入、持股大增和按公司汇总，按 issuer 归组；显示和报价用的代码取发行人 ticker 中第一个有效代码，没有时取申报 ticker 文本中的第一个。每笔价格先核对（`insider/prices.py`）：价格为空或 ≤ 0（期权行权、股票奖励等 $0 交易）时不核对、不显示核对标记；其余的参考价是 24 小时缓存里交易日的收盘价，没有时用最新报价，都没有标“未核对”；申报价与参考价相差达到配置的倍数（`price_mismatch_ratio`，默认 10 倍，随响应下发给界面文字）时照常显示申报价，但不计入金额排名、合计和公司汇总，并注明未计入的笔数。主查询只读部分索引 `ix_event_overview`（包含总览用到的全部列，其中申报 ticker 与交易后持股是 JSON 字段的存储生成列），不访问宽的交易行，磁盘冷时也不会超时。每个列表只返回前若干行和总数（`config/analysis-defaults.toml` 的 `[insider_overview]`），响应同时给出需要报价的代码，页面一次合并请求。
- **SEC 联系信息**（`sec/contact.py`）：SEC 要求自动访问在 User-Agent 中写名字和真实邮箱。`deploy/.env` 的 `IIRP_SEC_USER_AGENT` 有值时用它（界面显示“由配置文件设定”，不可改）；否则用界面填写、保存在 `user_preferences.values.sec_contact` 的名字和邮箱（`GET/PUT /api/v1/sec-contact`）。没有真实邮箱（空、旧模板值、`example.com` 等 RFC 2606 保留域名）时 SEC 策略保持开启但不发任何请求，首页顶部提示填写。保存后 worker 在数秒内（读取缓存 5 秒）开始轮询，不需要重启。
- **数据页健康检查**（`api/health.py`）：只看当前问题——worker、SEC 联系方式与轮询、来源冷却、磁盘、备份、最近 24 小时失败的任务（按类型和原因分组，可一键重试）；没有问题时只显示一行“All systems normal”。超过 7 天未处理的部分完成批次归入“已完成”。

### 界面文字

后端不返回某种语言的成句文字。报错、状态说明、提示、任务标题和方法说明都是消息 `{"code": "...", "params": {...}}`（`iirp.messages.msg`）；放在原来的文本字段（数据库列、异常、子进程结果、JSON 响应）里时是该对象的紧凑 JSON 字符串，HTTP 报错的 `detail` 直接是对象。前端用 [i18next](https://www.i18next.com/) / react-i18next 按代码显示：翻译在 `frontend/src/locales/{en,zh}/translation.json`（`{{参数}}` 插值；`ui.*` 是界面自身的文字）。首次打开为英文，右上角切换 English / 中文，选择存在浏览器的 localStorage；数字、日期按语言格式化（`lib/format.ts`），时间统一按美东显示并注明时区，悬停可看本地时间。前端在显示时翻译（`tm`），切换语言不需要重新请求。`tests/test_messages.py` 检查后端用到的每个代码在两种语言里都有翻译；`scripts/check_ui_text.py`（在 pytest 中运行）拒绝前端源码里写死的中文字符和成句英文。

## 数据流

1. **采集**：用户在界面提交需求（或启用自动策略）→ API 写入批次与任务 → worker 领取任务 → 访问外部来源（网络调用不在数据库事务内）→ 原文按内容哈希存入 `runtime/objects`，解析结果在一次带围栏的事务中提交为事实。
2. **SEC 内部人交易**：最新申报由 worker 按来源轮询（`iirp/sec/poll.py`，状态是 `source_poll` 中每个来源一行：水位线、未读完的补扫游标、下次时间、租约），不为每次轮询建任务；历史回补用 `sec_discover` 扫日/季索引 → `sec_document` 下载并解析 Form 3/4/5 → 写入申报、版本、申报人和按行的交易事实；修订按行记录，不覆盖旧版本。交易日、接受时间和发现时间分开保存；缺失、未知和已知零分别表示。
3. **行情**：`market_identity` 确认证券身份 → 规划时若该证券的缓存覆盖不了所需区间，`market_history` 一次取“所需区间 + 前 1 个月余量”到当天，替换为一份新的 24 小时缓存（`price_cache` + `price_cache_bar`，仅拆股调整的 OHLC、分红与拆股，记下获取与到期时间）→ `market_quote` 另行更新首页报价。到期的缓存由 worker 删除。
4. **分析**：用户显式应用条件 → 月度/区间由 `research_compute` 任务、财报/事件由规划器在行情缓存（及基准缓存，所有股票共用一次取价）就绪后直接读取缓存日线，计算并写入结果（记下所用缓存与获取时间）。结果与所用缓存同生命周期（`analysis_result.expires_at`，事件结果取股票与基准缓存中较早到期者），图表、表格、结论和导出（统计 CSV 与明细 CSV，图表用 ECharts 存 PNG）都读同一份结果；同一天相同条件（事件、n、基准）的事件分析直接复用，不再新建；过期后打开研究会自动按最近已完成交易日重新获取。
5. **读取**：所有 GET 只读本地数据库；信息流和详情在阅读会话中保持稳定，新内容以提示形式出现。

### 信息流的阅读会话

- 每个公司/日期分组的每次变化都追加一条不可变修订（`feed_group_revision`），带发布序号 `seq` 和写入事务号 `xid`。`feed_group_current` 指向每组最新修订；`feed_group_order` 为最新的非空修订保存排序键，每个“筛选 × 排序”一行。两张表在发布修订的同一事务最后一步更新。
- 打开信息流时只保存**水位线**（序号、`pg_current_snapshot()` 快照、打开事务自身的事务号）和筛选条件，不保存分组清单。修订属于该会话，当且仅当序号不超过水位线且写入事务在快照中已提交；这个集合以后不会再变，所以翻页（游标为排序键、接受时间、分组键）不重复、不遗漏，已加载的页面也不跳动。
- 水位线之后才变化的少数分组，按水位线读取当时的修订并合并到页面中；新内容只计数（“有 N 条新内容”），合并时在两条水位线之间算出确定的增量。
- 事务号只在同一 PostgreSQL 集群内有意义：web/worker 启动时若发现集群标识变化（换机恢复、升级），会清除已保存的事务号并丢弃旧快照的会话。
- 交易日期晚于该申报 SEC 接受日的行标为“日期异常、待核对”：保留原值并在详情中显示，不参与按实际交易日的排序和日期范围。

## 任务模型

- **持久任务**（`job`）：状态为 `QUEUED`、`RUNNING`、`PAUSE_REQUESTED`、`PAUSED`、`CANCEL_REQUESTED`、`CANCELLED`、`FAILED`、`RETRY_WAIT`、`PARTIAL`、`SUCCEEDED` 等；用户的暂停、恢复、取消、重试写为持久意图，由 worker 在安全点执行。
- **租约与围栏**：worker 领取任务时拿到租约令牌并持续心跳；每次提交事实都带令牌校验，租约丢失（`OwnershipLost`）则整笔事务放弃，过期租约由调度回收后重新排队。执行是至少一次，因此提交必须幂等。
- **批次与订阅**：一个用户需求对应一个批次（`batch`）；批次通过 `batch_job` 订阅任务。多个批次可以共享同一个计算任务（`analysis/shared_compute.py`），暂停或取消某个批次只影响它自己的订阅，仍有其他活跃订阅时共享任务继续运行。
- **依赖**：`job_dependency` 表达先后关系，前置任务未成功前后续任务不会被领取。
- **来源预算与限流**：`source_budget` 记录各来源的请求预算；所有外部请求走同一个传输入口，由调用方统一限速。
- **任务列表**：`GET /api/v1/jobs` 按 (创建时间, id) 键集分页、每页默认 20 条，只返回标量字段；`checkpoint`、`result`、`target` 只在 `GET /api/v1/jobs/{id}` 中返回。
- **主要任务类型**：`sec_discover`、`sec_identity`、`sec_document`、`market_identity`、`market_history`、`market_quote`、`research_compute`、`maintenance_backup`、`maintenance_clean`；数据页诊断另有开发模式下的小样验证 `fixture_check`、`sec_probe`、`market_probe`。

## 存储

- **PostgreSQL**：业务事实（`issuer`、`reporting_owner`、`security`、`filing`、`filing_version`、`transaction_event` 等）、24 小时行情缓存（`price_cache`、`price_cache_bar`）、信息流修订与索引（`feed_group_revision`、`feed_group_current`、`feed_group_order`）、任务与批次（`job`、`batch`、`batch_job`）、分析请求与结果（`analysis_request`、`analysis_result`、`export_manifest`）、事件集与提示词模板（`event_set`、`prompt_template`）、维护记录。
- **来源对象**：原始 XML/JSON/CSV 以内容哈希为文件名存放在 `runtime/objects/<前两位>/<哈希>`，数据库的 `source_object` 与 `source_observation` 记录来源、哈希和观察时间。可随时重新获取的内容带 `expires_at`：SEC 列表页、索引文件和 SEC 任务的响应 JSON 7 天，Yahoo 与计算任务的响应 JSON 24 小时；worker 每 10 分钟分批删除过期对象、其观察记录和文件。申报原文（`filing_version` 引用的完整申报、XML 和索引页）和解析出的事实永久保留；交易与申报的 JSONB 不再复制原始 XML，按 `source_objects` 引用读取原文。
- **存储盘点**：系统状态只读记录——原文大小取自 `source_object`，备份取自各自清单（按文件身份缓存，只解析一次），恢复副本取自其报告，数据库取自 PostgreSQL；不遍历目录。目录实际占用的精确核对（约 130 万个文件）在数据页手动触发，或由 worker 每天最多一次在美东 0–5 点运行。
- **卷**：Compose 的 `postgres-data`（数据库）与 `app-runtime`（来源对象、备份、维护日志）。`./iirp stop` 保留卷；备份由 `./iirp backup` 生成一致快照（`pg_dump` + 来源对象哈希清单），每次成功后只保留最近 2 份，`./iirp restore-verify` 在随机新库上验证恢复，通过后删除副本原文和恢复库，只留验证报告（写入备份清单，并在 `runtime/restores/<名称>-report.json`）。
- **测试**：`./iirp check` 和 `./iirp test` 为后端测试启动一个临时 PostgreSQL 容器（数据在 tmpfs、独立网络），跑完删除；测试只在其中的 `iirp_v1_test_*` 库运行，不在正式实例的数据库服务器上建库。
