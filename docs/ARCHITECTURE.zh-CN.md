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

Compose 启动三个服务：`postgres`、`web`（启动时先执行 `python -m iirp.migrate upgrade heads`）、`worker`。web 和 worker 使用同一个镜像。API 从不在请求中等待外部来源；浏览器关闭后 worker 继续工作。

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
   └──────┬────────► market（Yahoo 适配、长期日线与 24 小时缓存、报价）
          ▼
   models · db · config · messages
```

| 包 | 一句话说明 |
|---|---|
| `api/` | FastAPI 应用（`app.py`）、各领域路由（`research.py`、`events.py`）、本地只读投影（`reads.py`）和请求/响应模型（`schemas.py`，Insider 读取的响应模型在 `insider_schemas.py`）；Insider 查询、取价与前后 n 日在 `insider.py`，系统健康检查在 `health.py`；`docs/openapi.json` 由它导出。 |
| `jobs/` | 持久任务与租约（`queue.py`、`ownership.py`）、用户需求批次（`batches.py`、`batch_views.py`）、规划（`planner.py`）、执行（`worker.py`、`handlers.py`、`operations.py`、`operation_pool.py`）、自动更新与定时（`auto_update.py`、`schedule.py`）。 |
| `sec/` | SEC EDGAR：URL 校验与解析（`parse.py`、`ownership.py`）、限速下载（`fetch.py`）、历史范围规划（`planning.py`）、最新申报轮询（`poll.py`）、按 CIK 获取一家公司或一个人的申报（`entity.py`）、User-Agent 用的联系信息（`contact.py`）。 |
| `insider/` | 把申报写成按行的交易事实（`facts.py`），信息流的修订、水位线与阅读会话（`views.py`、`feed_index.py`、`feed.py`、`feed_updates.py`），公司/人员历史与交易详情（`entities.py`、`transactions.py`、`records.py`）、Insider 查询（`lookup.py`）。 |
| `market/` | Yahoo 适配器与日线校验（`yahoo.py`）、长期日线与删除起点（`long_prices.py`）、24 小时输入缓存（`cache.py`）、首页报价（`quotes.py`）、本地行情读取（`reads.py`）。 |
| `analysis/` | 所有金融计算：板块涨跌、周期锚点、热力图权重与周/月 K 聚合（`sectors.py`）；交易日历、月度与区间研究（`research.py`：每年一根“首个交易日开盘 → 最后交易日收盘”的 K 线、完整过去年份的统计、上涨比例的 Wilson 区间、同日期基准与超额）、分布统计（`distributions.py`）、事件窗口（`event_windows.py`）、交易前后窗口（单笔 `transaction_windows.py`，列表 `insider_windows.py`）；分析请求、结果复用与过期后重新获取。前端不重复实现。 |
| `events/` | 财报与自定义事件：两步式提示词模板（`prompts.py`）、AI-JSON 校验（`input.py`：格式问题逐条报错；背景信息超出所选范围、超过 8 条、缺来源或链接与“不确定”标记给警告，不阻止保存）、事件集与分析（`service.py`）。 |
| `storage/` | 按内容哈希保存来源文件（`objects.py`）、维护与备份调度（`maintenance.py`）、存储盘点与系统状态。 |
| `models/` | 全部表定义，按领域分文件（`insider.py`、`market.py`、`jobs.py`、`analysis.py`、`sources.py`、`watchlist.py`）；统一从 `iirp.models` 导入。 |
| `watchlist.py` | 自选股（`/api/v1/watchlist`）：代码按 Insider 标记的比较规则（`insider/activity.py` 的 `ticker_key`：大写，“.”与“-”相同）规范化后唯一，最多 500 个，存在 `watchlist_item`（随每周备份保存）；“知道了”的时间点存在 `user_preferences.values.watchlist`。列出时附本地已知的公司名（指数名单、已保存的证券、SEC 发行人），加入、导入和列出只读写本地表，不请求来源、不排任务；本地没有数据的代码照样保存。 |
| `notifications/` | 通知（`/api/v1/notifications`）：设置（`settings.py`，存在 `user_preferences.values.notifications`）、由本地新数据生成通知（`scan.py`，worker 每半分钟一次）、ntfy 手机推送任务（`push.py`）与推送文字（`texts.py`）。 |
| `notable/` | 关注的人（`/api/v1/notable`）：稳定标识与默认名单（`people.py`，`config/notable-people.toml` 只在名单为空且从未填过时填一次）、三个来源合并的交易时间线与个人页（`trades.py`）；只读本地表，不取数据。 |
| `messages.py` | 返回给用户的文字是“消息代码 + 参数”，见下文“界面文字”。 |
| `extensions.py` | 可选功能：`iirp` 下带 `extension` 模块的子包。应用、worker、调度和迁移只通过这里加载它们（路由、任务类型与通道、调度钩子、模型、迁移目录），不直接导入；缺了这个子包，其余部分照常运行。可选功能的路由不进 `docs/openapi.json`。迁移入口 `migrate.py`（`python -m iirp.migrate`）。 |

迁移在 `migrations/`（Alembic）。采集与保留的默认值在 `config/collection-defaults.toml`，刷新间隔在 `config/refresh.toml`，分析的 n 默认值在 `config/analysis-defaults.toml`，板块与 ETF 的对应和热力图参数在 `config/sector-map.toml`，来源契约在 `config/provider-contracts.json`。股票的别名（中文名、旧名、常用简称，例如“谷歌”“Facebook”）在 `config/aliases.toml`：手工维护、不联网、不自动生成，每个别名对应一个或几个代码（同一公司的多类股票都列出）；`iirp/aliases.py` 读取并检查格式（别名不分大小写不得重复、代码格式），`python -m iirp.aliases` 再核对代码都在已保存的指数名单里。顶部全站搜索（`api/reads.py` 的 `search`）先列别名命中的公司（同一公司的多类股票合为一项，公司按证券的发行人或指数名单的 CIK 找，标普 500 名单优先），再列名称与代码匹配的结果。

## 前端（`frontend/src`）

页面在 `pages/`，共享组件在 `components/`。已改造的部分用 Tailwind 和 [shadcn/ui](https://ui.shadcn.com/)（`components/ui/`，Radix 组件）：外壳在 `components/shell/`（侧栏、顶部栏、搜索、任务入口、语言切换、系统状态），首页在 `components/home/`（行情条 `MarketStrip`、Insider 瀑布流 `InsiderStream` 与卡片 `FeedCard`），Insider 查询 `pages/Insiders.tsx`、公司/人员页 `pages/Entity.tsx`、交易详情 `pages/Transaction.tsx`、数据与任务 `pages/Data.tsx`，它们共用 `components/insider/`（K 线 `PriceChart`、交易表 `TradeTable`、前后 n 日单元格、范围与 n 控件）。分析中心的外壳与月度/区间结果在 `components/analysis/`（四个页签、可收起的条件栏、逐只股票的分步进度、结论卡、年份 × 月份热力表、每年一根 K 线、排名与统计表、逐年明细、计算方法），月度与区间页是 `pages/Seasonal.tsx`，请求、轮询和 24 小时过期后自动重取在 `lib/analysis.ts`。财报与事件页是 `pages/Events.tsx`，组件在 `components/events/`（左侧输入流程 `EventInputs`：提示词、粘贴 JSON 与预览改时段、n 与基准、已保存事件集；结果 `EventResults`；图表 `EventCharts`：一个事件一根反应日 K 线、R−n…R+n 平均路径、单个事件日 K 线；表格 `EventTables`），请求与结论句在 `lib/events.ts`。行情详情页 `pages/Market.tsx`（行情条每个品种都可点开）显示保存的报价与最近 6 个月日 K 线。板块页是 `pages/Sectors.tsx`，组件在 `components/sectors/`（TanStack Table 数字表、行业组表 `SectorGroupTable`（参考行可展开）、ECharts treemap 热力图、`echarts.connect` 同步的纵向 K 线，只渲染进入可视区域的图），首页板块条是 `components/home/SectorStrip.tsx`（一行，按服务器的今日排序与名次 `ranks` 从左到右标 1–11），请求与 URL 状态在 `lib/sectors.ts`、`lib/sectorState.ts`。旧的 `legacy.css` 已全部删除，样式只用 Tailwind 与 shadcn/ui。

- **可选功能**：`src/features/<名称>/` 下有 `index.tsx` 的目录是可选功能。`src/features.ts` 用 `import.meta.glob` 收集它们的路由、侧栏入口（按 `order` 插在内置入口之间）；翻译由 i18next 后端按当前语言加载（`locales/<语言>.json`），`App.tsx` 与侧栏从这里读取；目录不存在就没有这个入口。可选功能的接口契约在它自己的 `openapi.json`，类型生成到它自己的 `generated/api.ts`（`npm run generate:api` 一并生成，`npm run check:api` 一并核对），用 `contractClient` 请求；前端测试另跑 `src/features/*/tests/*.test.mjs`。可选功能还可以在公共页面的股票代码旁加小标记（`Feature.tickerMark`，见 `lib/tickerMarks.tsx`）：页面用 `TickerBatch` 包住一张表、在代码旁放 `TickerMarks`，标记拿到代码和整张表的代码（每批最多 200 个），一批只发一次请求、全表共享缓存；没有安装这样的功能时什么都不渲染。首页瀑布流、Insider 总览各页签和公司页标题已放好位置。
- **自选股**：`lib/watchlist.ts` 读一次列表（`GET /api/v1/watchlist`，全站共用一个查询），星标按钮 `WatchStar`（公司页、行情详情页标题旁）按下即生效、失败时恢复并在旁边提示；表格里代码旁的小星标 `WatchMark` 只做标记（首页瀑布流、Insider 总览），不发额外请求。设置窗口的“自选股”页签列出代码、公司名与加入日期，可添加、删除、粘贴导入（逗号、空格、换行分隔，返回新增、已存在、格式无效）和复制全部；`openSettings("watchlist")` 从任何页面打开这一页签。首页瀑布流和 Insider 总览的“只看自选”（`WatchFilter`，写进链接 `?watchlist=true`）在自选为空时不可用、悬停说明在哪里添加；首页指数条与板块条下方的一行提示是 `components/watchlist/WatchlistNews.tsx`（最多列 3 只股票，其余“+n”；“查看”里每只股票按“人 + 买或卖”合并成一行——合计金额、笔数、交易日或日期范围，按金额从大到小、金额未知的在后，最多 5 行，其余“另 n 人”链接公司页；“知道了”记下时间）；瀑布流工具栏里的“只看自选”在区域窄于 80rem 时只显示星标和数字，名称在悬停里。
- **请求**：新代码用 [openapi-fetch](https://openapi-ts.dev/openapi-fetch/)（`lib/api-client.ts`），类型由 `docs/openapi.json` 生成到 `generated/api.ts`（`npm run generate:api`，`npm run check:api` 校验一致）；服务器状态用 TanStack Query。前端只展示后端计算好的结果，不重复实现金融计算。
- **配色**：令牌在 `index.css`。涨/买（`--up`）、跌/卖（`--down`）及其浅底和闪动色跟随设置：右上角齿轮打开设置窗口，预设为美股（绿涨红跌，默认）、色弱友好（蓝涨橙跌）、亚洲（红涨绿跌）或自定义两种颜色，一律带 + / − 号。选择存在 `user_preferences.values.display`（`GET/PUT /api/v1/display`，所有浏览器共用），浏览器另缓存一份，首屏直接用上次的颜色。`lib/colors.ts` 由选择算出全部涨跌色并写到根元素的 CSS 变量上，ECharts 图（K 线、热力图、箱图、事件路径）通过 `usePalette()` 读同一份值，图表的中性色也集中在那里。健康/实时状态（`--ok`）和选中行（`--selected`）有自己的颜色，不随涨跌预设变化；主操作色是近黑的墨色；数据延迟用紫灰（`--warn`），不与“跌”混淆。三个预设的文字色在卡片、页面背景以及自身浅底和闪动底色上的对比度都 ≥ 4.5:1（`frontend/tests/colors.test.mjs`）；自定义颜色低于 4.5:1 时设置窗口给出提示。目前只有浅色主题。
- **动画**：[Motion](https://motion.dev/) 负责新卡片落入、展开收起，[NumberFlow](https://number-flow.barvian.me/) 负责行情数字滚动（变化时背景按涨跌色闪动约 1 秒）；首次加载用骨架；后台刷新时顶部栏刷新图标转动并显示细进度线；数据延迟的标记缓慢呼吸。时长约 0.15–0.3 秒，系统开启“减少动态效果”时不做位移动画。
- **长列表与表格**：瀑布流用 [TanStack Virtual](https://tanstack.com/virtual) 按窗口虚拟化；交易表和任务表用 [TanStack Table](https://tanstack.com/table)；K 线用 ECharts。

### 首页刷新

间隔只写在 `config/refresh.toml`：SEC 轮询节奏、行情的开盘/盘前盘后/休市规则，以及浏览器端的间隔（重读本地行情、检查新申报、询问服务器是否该更新、重读 Insider 总览）。浏览器端间隔随 `/api/v1/home` 下发。外部请求只由 worker 发出；页面打开、回到前台和可见期间每隔一段时间调用 `POST /api/v1/freshness/ensure`，服务器用咨询锁、“60 秒内已请求”和每个品种的 `next_refresh_at` 合并请求，所以多个标签页不会让 SEC 或 Yahoo 请求成倍增加；标签页隐藏时不轮询。行情是否“交易中 / 已休市 / 延迟”在读取时按当时时间判断（`api/reads.py`），延迟会写明原因（超时未更新、来源报价滞后、最近一次更新失败、收盘价尚未取回）。

行情条的品种只列在 `market/instruments.py`：标普 500、纳斯达克综合、纳指 100、道琼斯、罗素 2000、VIX、10 年期美债收益率（^TNX，Yahoo 以 % 报价，`/home` 另给 `change_bp` 基点变化）、美元指数（DX-Y.NYB）、黄金与 WTI 原油期货、标普与纳指期货（ES=F、NQ=F）、比特币；默认显示标普 500、纳指 100、道琼斯、罗素 2000、VIX、10 年期美债、黄金与 WTI 八项，其余默认关闭。显示哪些是显示设置 `display.market_strip`（设置窗口勾选），未勾选的既不显示也不获取。每个品种的交易时段取自它自己的 Yahoo 报价（`currentTradingPeriod`），不套用股票日历：交易中每分钟、美股盘前盘后每 5 分钟，休市等下一个窗口；期货类休市时另在 CME/ICE 周日至周四 18:00（美东）重新开盘时到期，所以周末后的周日晚上就会更新。ES=F、NQ=F 在美股常规时段（NYSE 日历，含提前收盘）内不显示也不获取，时段外只在保存的期货报价仍处于其交易时段时显示；浏览器另按 CME Globex 周日 18:00 至周五 17:00（美东，每日 17:00–18:00 暂停）和最近成交时间再判断一次（`frontend/src/lib/marketHours.ts`），周末和休市时不显示。所有到期品种仍由一次 `market_quotes` 批次获取（worker 按品种取日线快照），打开页面只取已到期的品种，“立即检查”取全部已勾选的品种。

### 板块

- **映射**：`config/sector-map.toml` 列出 11 个大板块和代表 ETF（SPDR 板块 ETF，代表标普 500 的对应板块，不覆盖全部美股），以及每个板块的 GICS 行业组（共 25 个）：主 ETF（唯一参与排名）、备选 ETF（只作参考行）、覆盖程度 complete/partial/reference/pending。加载时校验（`check_sector_map`）：pending 当且仅当没有主 ETF；`same_as_sector` 只用于板块唯一的行业组且主 ETF 就是板块 ETF（能源、原材料、公用事业）；除此之外同一只 ETF 不出现两次。
- **行业组**（`GET /api/v1/sectors/groups?sector=…`，不带 `sector` 为全部 25 个）：周期口径与大板块相同；排名（`ranks`）和热力图权重只在本次显示的主 ETF 之间计算，参考行和 pending 行不排名，pending 排在最后。行业组 ETF 只由 `POST /api/v1/sectors/prices`（`level=group`，可带 `sector`）请求，首页和大板块层级从不请求；每只 ETF 的获取进度（就绪 / 获取中 / 无数据 / 未取到）取自当天 `market_history` 批次的请求范围。K 线用 `GET /api/v1/sectors/candles?level=group`，只含有主 ETF 的行业组。
- **数据**（`analysis/sectors.py`）：日线优先读取覆盖需求的长期行情（长期库从过去第 10 个完整年的 1 月 1 日起保存，与板块窗口起点相同；完整保存这一段的 ETF 视为覆盖）；覆盖不足时，`POST /api/v1/sectors/prices` 为需要补充的 ETF 建一个 24 小时缓存批次，每只 ETF 一次取“今年 + 过去 10 个完整年”（`config/sector-map.toml` 的 `[history] years`，缓存另加 1 个月余量），同一天到下一次收盘前重复请求返回同一批次；月度/区间分析打开这些 ETF 时直接复用。上市晚于窗口起点的 ETF（如 XLC）按实际年份计数，不补造历史。报价走共享的批量股票报价（`POST /api/v1/insider/quotes`，一次请求全部代码），节奏同首页指数（开盘 60 秒、盘前盘后 5 分钟、休市不刷新）。`GET /api/v1/sectors` 与 `GET /api/v1/sectors/candles` 只读本地缓存，切换视图、周期、范围和排序不会触发外部请求。
- **口径**：价格收益，仅拆股调整。1 周/1 个月/3 个月的终点是缓存中最近完成交易日 L 的收盘，起点是“L 往前 7 天/1 个月/3 个月那天或之前最近交易日”的收盘（3/31 往前 1 个月为 2/28 或 2/29）；年初至今的起点是上一年最后交易日收盘。今日：常规时段用最新常规时段报价对比前一交易日收盘（标“盘中 · 更新时间”）；其余时间用 L 收盘对比前一交易日收盘。热力图面积权重 = 0.2 + 0.8 × (r − r_min) / max(r_max − r_min, s)，s 按周期取 1%/2%/4%/8%/8%。周 K 按周一至周五、月 K 按自然月由日线聚合（开 = 首日开盘、收 = 末日收盘、高低取极值）。
- **轮动**（`analysis/rotation.py`，`GET /api/v1/sectors/rotation`）：11 个大板块，或有主 ETF 的 18 个行业组（待补充的不参与）。每只 ETF 直接调用月度/区间研究引擎 `compute_research`（首个交易日开盘 → 最后交易日收盘；只统计完整过去年份，今年单列；中位、均值、四分位、上涨 x/N 与 Wilson 95% 区间；^GSPC 用同一首尾交易日算超额与跑赢 x/N），参数为 `years`（1–10，默认 10）、`kind`（`monthly` 或带 `start`/`end` 的 `interval`，可跨年）。在此之上按中位涨幅（`rank_by=absolute`）或相对标普的超额中位（`relative`）在 ETF 之间排名，相同数值同名次；每年只在当年有值的 ETF 之间排名，一只 ETF 上方不足 3 只即为当年前三、下方不足 3 只即为后三，统计进入前三 / 后三的年数（分母为它有值的完整年数），并给出每年的第一名与最后一名。上市较晚（XLC）或长期库起点之后才有数据的年份按实际 N 统计。价格只读长期库，不读 24 小时缓存、不建任务、不发外部请求。每个周期返回名次和中位等摘要，完整行（统计、箱图分布、逐年值与名次、前三 / 后三）只返回 `month` 选中的月份（默认本月）或区间；比例保留 10 位小数。计算结果按条件与所用长期序列（代次、完整至、获取时间）在 web 进程内保留 24 小时，到期或长期库变化后用本地数据重算。

### 瀑布流

信息流按“公司 × SEC 接受日（美东）”分组，每组里每位申报人一行：谁（职务）、交易代码对应的 SEC 说法、股数、金额（买入为 +、卖出为 −）、交易日；卡片右上角是披露时间。列表在服务器的阅读会话（水位线，见下文“信息流的阅读会话”）上工作：读者停在顶部时，新申报读取两条水位线之间的增量后立即落入；读者往下翻时列表不动，顶部出现“↑ N new trades”，点击后先回到顶部再落入；接近底部时用同一会话的游标加载更早的组。列表操作是纯函数（`lib/feedStream.ts`），有单元测试。

### 指数名单与股票所属板块

- **名单**（`market/constituents.py`）：标普 500 取自 GitHub datasets 的 CSV（含 CIK、GICS 板块与子行业），纳指 100 取自 Wikipedia 成分表（ICB 行业与子行业原样保存，`classification` 列注明 GICS/ICB）。每周固定时间更新一次（`config/refresh.toml` 的 `[index_constituents]`，默认周六 08:00 美东）：上次成功早于最近一个计划时间的名单到期；失败保留上一份名单和更新日期，`retry_seconds`（默认 6 小时）后重试。数据页“指数名单”显示成分数、上次更新、下次时间和失败原因（`GET /api/v1/index-constituents`），“立即更新”（`POST …/refresh`）为每份名单建一个任务，已有进行中的任务时不重复建。
- **成分标记**：`market/classification.py` 的 `Memberships` 一次读出两份名单，按 CIK 或 ticker（`.` 与 `-` 视为相同）判断，在读取时给瀑布流分组、Insider 总览各行、查询结果（本地与 SEC 候选）和公司页加 `indexes`，前端显示 SPX / NDX。
- **股票 → 板块**（`GET /api/v1/securities/{symbol}/classification`）：① 在标普 500 名单里：名单的 GICS 板块，子行业按 `config/gics.toml`（MSCI 2023 年 GICS 结构：163 个子行业 → 25 个行业组，另列 2023 年前的旧名）对到行业组；② 否则用 Yahoo 的 sector：身份核对（`market_identity`）时 `get_info` 已返回，多存 `sector`、`industry` 两个字段，不增加请求，名称按 `config/sector-map.toml` 的 `[yahoo_sectors]` 对到 11 个板块，没有行业组；③ 都没有则无板块。结果带来源（`sp500_gics` / `yahoo` / `none`）、板块 ETF、行业组主 ETF（pending 的行业组没有）。身份核对早于存 sector 的证券返回 `refresh: needed`，公司页打开时调用一次 `POST …/classification/refresh` 排一个身份刷新，不批量补。纳指 100 的 ICB 分类不用于映射。公司页标题下显示一行板块。
- **批量的已保存分类**（`market/classification.py` 的 `SavedSectors`）：给 Insider 汇总和筛选用，顺序同上——标普 500 名单的 GICS 板块与行业组（公司按 CIK、否则按代码匹配名单，“.”与“-”相同），否则身份核对时保存的 Yahoo 板块（没有行业组），都没有为未分类；只读已保存的数据，不排身份刷新。

### Insider 查询与前后 n 日

- **查询**（`insider/lookup.py`）：先查本地（`issuer.ticker` 精确匹配，或名字包含每个词）；本地没有时由 worker 的 `sec_identity` 任务去 SEC 查：像 ticker 的输入查 `company_tickers_exchange.json`，名字查 EDGAR 公司与人员搜索（`efts.sec.gov/LATEST/search-index?keysTyped=…`，即 sec.gov 搜索框背后的公开接口），同一查询每天只请求一次。选中一项后建 `sec_entity` 批次：`sec_discover`（mode=entity）读该 CIK 的 `data.sec.gov/submissions` 文件（Form 3/4/5 同时列在发行人和每位申报人名下），只为范围内的申报建 `sec_document` 任务。全部走现有 SEC 限速（每秒 ≤ 2 次）和 User-Agent。
- **前后 n 日**（`analysis/insider_windows.py`）：以交易日 t（非交易日顺延）收盘为基准，前 n 日 = C(t)/C(t−n) − 1，后 n 日 = C(t+n)/C(t) − 1；尚未到来的交易日留空并给出预计日期，当天未收盘标“盘中”。价格优先读取申报 ticker 的长期日线，其他股票及覆盖不足的区间使用 24 小时缓存；该证券未经 SEC ticker 列表确认属于该发行人时照常计算并标“待核对”。`GET /insider/windows` 只读缓存；缺价时页面调用一次 `POST /insider/prices`，每只股票一次请求取“默认 6 个月与最早 t−n 的较早者 − 1 个月余量”到当天，所以随后打开公司页不再取价。n 默认值在 `config/analysis-defaults.toml`，页面上切换并记住。
- **总览**（`insider/overview.py`，`/insider` 页）：多人买入/卖出、最大买入/卖出、高管买入、持股大增和按公司汇总，按 issuer 归组；显示和报价用的代码取发行人 ticker 中第一个有效代码，没有时取申报 ticker 文本中的第一个。每笔价格先核对（`insider/prices.py`）：价格为空或 ≤ 0（期权行权、股票奖励等 $0 交易）时不核对、不显示核对标记；其余的参考价是 24 小时缓存里交易日的收盘价，没有时用最新报价，都没有标“未核对”；申报价与参考价相差达到配置的倍数（`price_mismatch_ratio`，默认 10 倍，随响应下发给界面文字）时照常显示申报价，但不计入金额排名、合计和公司汇总，并注明未计入的笔数。主查询只读部分索引 `ix_event_overview`（包含总览用到的全部列，其中申报 ticker 与交易后持股是 JSON 字段的存储生成列），不访问宽的交易行，磁盘冷时也不会超时。每个列表只返回前若干行和总数（`config/analysis-defaults.toml` 的 `[insider_overview]`），响应同时给出需要报价的代码，页面一次合并请求。
- **代码旁的 Insider 活动**（`insider/activity.py`，`GET /api/v1/insider/activity?tickers=&days=30`）：一批代码（最多 200 个）近 N 天的公开市场买卖汇总——买入与卖出各自的人数、笔数、金额，净额，最近交易日，其中 10b5-1 计划交易的笔数，以及金额最大的 3 位交易人。行、窗口和金额的规则与总览相同（共用 `_rows` 与 `_priced`）；交易的发行人代码或申报所写代码（多个类别时任一个）与请求的代码相同即计入，“.”与“-”视为相同。只读本地库，不返回、也不触发报价或取价。
- **板块与 Insider**：总览可按公司当前的板块筛选（`sector=` 为板块 id 或 `unclassified`，对所有列表生效，与指数筛选同时使用；在覆盖索引读出的行上按 `SavedSectors` 过滤，SQL 不变）。`GET /api/v1/insider/sectors?days=30` 按同样的分类汇总近 N 天的公开市场买卖：11 个大板块与 25 个行业组各自的买入、卖出、净额和公司数，未分类公司单列；行业组只统计有 GICS 子行业的公司，每个大板块另报没有子行业的公司数。板块页表现表（两级）的“30 日 Insider 净买入”列读它，可排序（`sort=insider`）；主 ETF 列默认收进列设置，代码与覆盖程度显示在名称下。
- **只看自选与自选股提示**：瀑布流 `GET /api/v1/feed?watchlist=true` 在打开阅读会话时把当时的自选代码存进会话（`feed_session.filters.watchlist`），之后的分页、更新计数和增量都只看这些公司（发行人 ticker 的任一类别与自选代码相同，“.”与“-”相同），自选股变了由页面另开会话；每次请求先读出这些公司（发行人）和它们的分组键（`ix_feed_group_current_issuer`，迁移 0008），再按 `feed_group_order` 主键取这一页，可与指数筛选同用。总览 `watchlist=true` 把自选代码交给 `_rows` 的代码筛选（与 Insider 活动同一条件，仍走 `ix_event_overview` 的 index-only scan），对所有页签生效，可与指数、板块同用。首页提示 `GET /api/v1/watchlist/news`（`insider/activity.py` 的 `new_trades`）：每个自选代码从“加入时间”和“上次知道了”中较晚的一个算起，列出之后被 SEC 接收的公开市场买卖（行口径同总览），按申报人与买卖方向合并成行（每只股票返回金额最大的 5 行和总行数，笔数仍逐笔统计）；修正申报带着原申报的接收时间，不会第二次算作新内容；最多回看 30 天。`PUT /api/v1/watchlist/acknowledged` 存页面读到的时间（只前进，不晚于现在）。可选功能经前端 `Feature.watchlistNews`（`lib/watchlistNews.ts`）补充提示条目，没有安装时提示只含 Insider 条目；没有新内容时不渲染。全部只读本地数据。
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

### 分析的两个参照

月度、区间、财报与事件请求保存 `benchmark`（大盘）和 `sector_benchmark`（所属板块）。大盘默认 ^GSPC，可选 ^NDX、不比较或其他 ETF；所属板块默认 `auto`，按已有 `classify` 服务逐只股票解析，也可选 `industry_group`、不比较或自定义 ETF。行业组没有主 ETF 时退回板块 ETF并带原因；GICS、Yahoo、无映射及自定义来源随结果保存。需要身份刷新的映射通过分析范围订阅持久 `market_identity` 任务，完成后才计算板块对比；失败带明确原因。

板块参照只使用本地长期行情或尚未过期的缓存，选择或切换板块参照不排外部取价任务。本地没有可用 ETF 时结果说明原因。两端日期与股票严格一致；月度/区间使用首日开盘到末日收盘，事件的事前、跳空、反应日、事后分别使用各自边界。收益、两列超额、跑赢计数及绝对/相对大盘/相对板块的分布均由后端计算；缺失值不当作零。板块输入纳入计算身份、发布围栏及最早到期时间，统计和明细 CSV 包含 ETF、来源、收益和超额。

新计算版本为 `research-v12-sector` 与 `event-windows-v3-sector`。旧结果的新增字段可缺省，读取注明不含板块对比；重新打开或切换参照通过已有分析变体重新计算。图表的大盘参照为虚线，板块参照为点线，涨跌颜色沿用显示设置。

### 事件的背景信息与分组

粘贴的事件可带 AI 整理的背景信息（`context`）、财报预期（`earnings`）和“不确定”标记，保存在事件集里，分析时随事件冻结；全部标“由 AI 整理，未经核实”，软件不核实也不联网检查。计算版本 `event-windows-v4-context`（`analysis/event_windows.py`）：每个事件的行另存这些字段、反应日前后配置交易日数的宏观窗口，以及覆盖 R−1−n…R+n 与所有背景日期的日 K 线（`context_candles`）；取价范围相应扩到最早、最晚的背景日期。`project_result` 读取冻结的行，按 (实际 − 预期) / |预期| 算每股收益与营收的超预期幅度，财报按每股收益分为超预期 / 符合 / 低于 / 无预期数据、按指引分组，每组给四个窗口的完整统计；`exclude_macro` 排除宏观窗口内有 `major`、类别为 macro / policy / geopolitical 的背景条目的事件后重算全部统计并给出排除数。阈值、交易日数和类别在 `config/analysis-defaults.toml` 的 `[event_groups]`。旧版本结果缺少的字段按空值读取（无背景、无预期数据）。前端组件在 `components/events/EventContext.tsx`（背景列表、时间线、预期对比表、分组统计），单事件 K 线的背景标记在 `EventCharts.tsx`。

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

### 长期行情数据

`long_price_series` 保存代码与证券、成员状态、移出日期、保留起点和控制代次；`long_price_bar` 保存拆股调整后的 OHLC、量与公司行动，只接受已完成的 NYSE 交易日。范围来自现有两份指数名单的并集、`sector-map.toml` 全部 ETF（含参考行）及 ^GSPC、^IXIC、^NDX、SPY、QQQ。名单更新后新成员补数，移出成员停更并保留历史。

`config/refresh.toml` 的 `[long_prices]` 配置保存年数（10 个完整的过去年份，从第一年的 1 月 1 日起，例如 2026 年为 2016-01-01；每年 1 月 1 日起点前移一年，更早的日线删除）、美东 17:30、最近 10 个交易日及比较容差。自动批次仍为 `market_history`，每只证券按顺序执行身份核对与 `market_long_history`，共用 Yahoo 单并发、来源冷却与任务租约围栏，低于页面即时取价优先级。批次与各证券进度在数据与任务页可见，重启后续接。每日仅比较重叠 OHLC，忽略分红调整的 `adj_close`；差异超过容差时保留旧数据，重新获取整个保留区间后原子替换，不保留价格版本。来源缺失或无效日线的日期存于 `details.missing_dates`；整段复核仍为同一组日期时存为 `accepted_missing_dates`，代码计入就绪并停止补数重试。每日窗口保留窗口外的缺口，新增或修复缺口重新整段核对。接受缺口只表示无需重取，分析仍按缺失日线处理并标注日期。

长期数据覆盖需求时，规划器从本地日线生成一份现有 24 小时输入缓存，月度、区间、财报、事件共享原有计算和结果到期规则，不建外部取价任务；板块与 Insider 只读路径也直接读取长期日线。未覆盖需求仍走现有 24 小时缓存路径。长期日线不受缓存清除任务影响。`adj_close` 只在原 Yahoo 响应和短期缓存保留，不用于金融计算，也不存入长期日线。

设置中的管理操作创建 `market_long_manage` 持久任务，由 worker 在围栏内提交；四种删除更新保留起点与控制代次，旧代次下载不能恢复已删除日线。“重新补齐”清除删除起点，恢复随年份前移的默认起点。保存起点晚于当前默认起点、且不是因用户删除而后移的序列（例如按旧规则从“今天往前 10 年”开始保存的），规划时代次加一并整段重取一次，走同一低优先级补数流程。自动补数与每日更新跟随行情自动更新开关。备份排除 `long_price_bar` 数据，但保留结构与 `long_price_series` 控制；恢复后没有日线的证券由补数任务重建，不越过保存的删除起点。


## 浏览器冒烟测试

`./iirp e2e` 在临时完整栈运行 Chromium / Playwright，不并入 `check`。PostgreSQL 的数据在 tmpfs，数据库名为 `iirp_v1_test_*`，网络没有外部出口；web 执行全部迁移并直接提供构建好的 `frontend/dist`，不启动 worker。示例申报、公司与人员、长期日线、分析和自选股均为合成数据，不代表真实来源覆盖。Freezegun 固定 web（包括同步路由的工作线程）的日期时间，Chromium 使用相同的固定时钟；盘中、收盘后与周末各运行中英文页面和常用交互，断言接口时钟一致、控制台无错误、API 无失败和浏览器没有非本机请求。

公共测试在 `frontend/e2e/`；可选功能的 `e2e/*.spec.mjs` 和 `e2e/seed.py` 放在自己的目录，只有安装时才发现。失败 trace、截图、web 日志和三次冷缓存首页计时保存在仓库外，命令始终打印路径；退出或中断后删除临时容器及网络。CI 用独立 PostgreSQL 服务、uv 环境和缓存的 Chromium 运行同一组场景，失败证据保留 7 天。


### 前端构建与资源加载

Vite 保留路由懒加载，并将 React / Router、TanStack Query / Virtual / Table、Radix、i18next、ECharts 与 zrender 分为独立依赖块；ECharts 再分为核心、图表类型与组件三块（核心引用的共用辅助模块留在核心块，块之间不循环）。`npm run build` 最后运行 `check-chunks.mjs`：任何 JS 块压缩后超过 500 kB 即构建失败（`./iirp check` 与 CI 都经过它），不调高警告阈值。带内容哈希的 `/assets/` 成功响应缓存一年并标为 immutable，HTML 继续 revalidate，API 继续 no-store。ECharts 只随图表页面加载，首页的行情条和板块条是数字与文字。首页使用 Motion 的 `m` 与 `domAnimation`，板块页和分析进度另加载 `domMax` 保留行的位置动画。设置、系统状态、SEC 联系表单、任务抽屉、列菜单和自选股提示的详情在打开时加载；路由与弹层等待时显示骨架。

`i18next-resources-to-backend` 加载当前语言及当前页面的 namespace；核心与可选功能的首屏翻译并行加载；切换语言时仍显示已经就绪的文字，资源完成后统一切换。`generate-locales.mjs` 从唯一维护的 `locales/{en,zh}/translation.json` 生成 `generated/locales/` 与静态 import 映射；派生文件不手工修改，`npm test` 核对同步。外壳和首页不经过 `PageLanguage`，只有入口 namespace；首页与 Insider 页面共用的交易价、现价和行情状态文字因此放在入口。`tests/entryLocales.test.mjs` 打包外壳与首页（摇树，并去掉在 `PageLanguage` 里懒加载的部分），要求其中出现的每个键都在入口 namespace。API 消息需要的 namespace 也由官方 `loadNamespaces` 在数据交给组件前加载。共享组件必须显式声明并等待全部文字所在的 namespace，不能把其他接口触发的加载当作依赖保证；例如数据页的自动更新与 SEC 状态摘要用 `useTranslation(["data", "settings", "translation"])`，在资源就绪前沿用路由骨架。旧数据库中文文字的识别规则另由 `generate-legacy-messages.mjs` 生成，只在 API 含中文原文时动态加载，不读取整份另一语言的字典。

可选功能保留轻量注册文件和首屏代码旁标记；在自己的 `locales/entry-keys.json` 声明首屏翻译前缀，构建时生成当前语言的轻量入口资源（没有声明时只取导航与页名）。完整翻译随该功能的 namespace 加载，非首屏详情用 React `lazy` / `Suspense`。浏览器夹具在每份文档中监听可见文字变化，任何键名闪现都会失败；合成数据给首页的股票写入实时 / 收盘、延迟和缺失三种现价，首页的行情状态行与价格提示都会显示出来；盘中场景逐页完整冷导航（关闭 HTTP 缓存并重建翻译内存状态），并以不预加载设置消息的健康响应及延迟翻译请求验证 SEC 摘要。语言和资源加载边界的浏览器断言在盘中场景各语言运行一次；页面、交互、切换语言和时钟断言仍覆盖盘中、收盘后与周末。

### 政府交易披露

`political/` 独立采集 House/Senate PTR 与 OGE 278-T。事实在公共的 `political_person`、`political_report`、`political_transaction` 中，`political_page` 保存按页解析进度；迁移 0009。原文件下载后先保存为永久来源对象，再检查 PDF 页数；检查或解析失败也保留原件。调度优先安排当前解析版本的待解析页，再安排页清单与下载；下载、清单各最多排队配置中的批量报告数（默认 8），避免回补下载挤占页面处理。`political_index`、`political_document` 在独立网络通道，`political_page` 在单并发低优先级 OCR 通道，沿用任务租约、围栏与子进程时限。分页索引游标、来源状态与节奏存在 `source_poll`，各网站请求预算存在 `source_budget`，同站咨询锁串行请求，网络不在数据库事务中。配置见 `config/political-disclosures.toml`。

`GET /api/v1/political/transactions` 按人物、代码、来源、买卖、交易/报告日期范围筛选并按报告日倒序分页（`report_date` 与来源单独返回，OGE 官方索引报告日期不冒充提交日 `filed_date`）；`people` 给报告数与最近报告日，`reports/{id}` 给原文链接、各行及需核对行，`reports/{id}/file` 读取已保存原文件；`sources` 只读来源与 OCR 状态；`POST /api/v1/political/sources/{source}/refresh` 复用正在进行的检查任务或新建手动任务，沿用回补游标。从未检查的来源立即排队；启动时尚未成功过的来源优先尝试一次，此后遵守间隔和失败退避。所有 GET 不触发来源请求。原报告与明确修正永久保留，读取不重复计算原报告；未关联修正不进入交易合计。数据与任务页按来源显示检查时间、报告/交易/核对数及 OCR 用时。美国国债可豁免申报，披露文件不包含所有交易；原文已申报的条目仍永久保留。


OGE 方向格逐格使用字符白名单 OCR，在固定交易选项之间做有距离上限的编辑距离匹配；有歧义时留未知并标核对。有可用文字层时以文字层为主，方向与 OCR 交叉核对，不一致时留未知。无代码股票只与本地指数名单、SEC 发行人和证券名称匹配，规范化后唯一精确匹配标 `local_exact`（名称匹配），唯一且超过配置阈值的模糊结果标 `inferred`（推断）；原名称永久保留，多个候选不填。债券、票据与基金单独识别，债券代码只保存于 `issuer_ticker`，不写进股票代码；公共迁移 0010。

解析版本升级后，已保存原件由同一低优先级 OCR 通道创建新版清单与分页任务，不重新下载。每页围栏内原子替换，行 ID 由报告、页和行号确定；新版已完成页在重启后保留，旧版任务不能覆盖新版事实。索引游标、未下载报告和正常回补继续保留。

### 关注的人

被关注的人存在公共表 `notable_person`（迁移 0011），每行一个稳定标识和加入时间，不用数据库生成的 id：SEC 申报人 `sec:<10 位 CIK>`，众议员 `house:<姓名>|<选区>`，OGE 申报人 `oge:<姓名>|<职务>`（姓名、选区、职务都按披露索引的写法，与 `political_person` 对应）。名单是用户数据，随每周备份保存。`config/notable-people.toml` 是可以手工编辑的默认名单（来源、标识、显示名、类别、中英文一句身份说明），web 与 worker 启动时若名单为空且从未填过（`user_preferences.values.notable.filled_at`）就填一次；之后以数据库为准，“恢复默认名单”只加回被移除的默认人物。`python -m iirp.notable.people` 检查文件（重复、标识缺失、CIK 格式）。

`GET /api/v1/notable/trades` 把三个来源合成一条时间线：SEC 是可见申报中当前有效的非衍生表公开市场 P/S 行，按 `owner_ids` 匹配 CIK（迁移 0011 的 GIN 索引 `ix_event_owner_ids`），公布日是 SEC 接收时间的美东日期，金额为申报股数乘申报价格；众议院与 OGE 用与政府披露接口相同的有效报告（排除被取代和未关联的修正），公布日是报告日期，金额是区间。排序键为（公布日，来源 SEC > 众议院 > OGE，固定标识），游标分页不重复、不遗漏。“只看关注”只取名单里的人；“全部”是本机所有众议员和 OGE 申报人，加上默认名单与名单中的 SEC 申报人。扫描件识别的行标 `ocr`；`review` 只列具体疑点：方向未知或经过校正、代码按名称推断、日期或金额缺失、交易日晚于报告或早于报告一年以上、金额不是规定区间、期权条款不全、资产名称少于三个字母、文字乱码。个人页（`GET /api/v1/notable/person`）给出身份、本机申报或报告列表与原文链接。

股价反应：每行给出前后窗口用的代码 `price_ticker`（股票本身；期权为报告写明的标的股票；债券、基金、类型不明和没有代码的行为空，`price_none` 写原因）和公布基准 `published_basis`。窗口与 Insider 页相同，由 `analysis/insider_windows.py` 的 `trade_windows` 计算：页面对屏幕上可见的行（`useVisibleStocks`）把（代码，交易日）和（代码，公布基准）交给 `/api/v1/insider/windows`，前者的前 / 后 n 日即交易前 / 后 n 日，后者的后 n 日即公布后 n 日；缺价时同一钩子调用一次 `/api/v1/insider/prices`（24 小时缓存，现有限速）。公布基准与 SEC 交易详情页的披露基准同一规则（`calendar.reaction_session`）：盘后接收的申报从当天收盘算，盘前或盘中接收的从前一个交易日收盘算，只有日期的众议院与 OGE 报告从报告日前一个交易日收盘算。`trade_windows` 只读出这些交易前后的日线（与按日查找的结果相同）。政界交易详情 `GET /api/v1/notable/trades/{id}` 给出该行、报告与原文行，页面 `/notable/trades/:id` 用同样的窗口与 `/api/v1/insider/bars` 画 K 线（交易日、公布日竖线，前后 n 日着色）。个人页汇总 `GET /api/v1/notable/summary?key=&n=`（`person_summary`）对此人最新 5000 行中有代码的买卖逐笔取同样的窗口，买入、卖出与交易后、公布后分开给出 N、上涨次数和中位数（N < 5 只列数值）；只用本机已有价格，没有价格或未满 n 日的笔数单列，不取价、不排任务。公司页的小块用同一接口按发行人和代码读近 90 天、最多 5 行。


时间线和个人页读取 `GET /api/v1/notable/reports`，以一份 SEC 申报或政界报告为一组，按组分页；股票、买卖、人和公布日期筛选只保留匹配行，无匹配行的组不显示。默认 `assets=equities` 只列股票与期权（包括没有代码的股票），`assets=all` 保留全部资产；切换写进 URL。组头始终显示该人物范围内整份报告的资产笔数和股票/期权买卖数，默认列出匹配股票/期权中金额最大的三笔（SEC 准确金额、政界区间下限，未知金额排后），只有其他资产时列出前三笔；展开可读全部匹配行。汇总和公司页仍读取原有逐笔口径。

政府报告解析版本 `political-v4` 在文字层、表格和 OCR 网格中排除收件章、电子签名、页眉页脚、表头、页码和交易说明脚注。只匹配明确的表单文字，不因方向、日期或金额缺失而删除真实交易；损坏的资产名称即使只剩数字，也保留源行中的交易证据。版本变更沿用已保存原件的清单与逐页任务，在低优先级带围栏通道重新解析并原子替换事实，不重新下载。

### 通知

通知默认关闭；`notifications.enabled_at` 记下开启时刻，此前的交易不通知。worker 主循环每 30 秒调用 `notifications/scan.py` 的 `tick`：在 `pg_try_advisory_xact_lock` 下单事务扫描，每条通知有唯一的 `dedupe_key`，`INSERT … ON CONFLICT DO NOTHING`，所以重解析、重启或重叠的扫描都不会重复。表 `notification`（迁移 0012）只存公开事实（谁、哪只股票、买卖、金额、交易日、公布日、原件公网链接）和已读时间、推送状态；`notification_push` 记下每条发出的消息（单条、摘要、测试）。

- 关注的人：SEC 一份申报一条（`trade:sec:<accession>`），条件是含被关注申报人的当前有效公开市场 P/S 行，SEC 接收时间晚于开启时刻和此人加入时间，最多回看 30 天；同一份申报有几位被关注的人时归到加入最早的那位；Form 4/A 标“修正”。众议院与 OGE 一份报告一条（`trade:political:<report>`），条件是全部页面读完（`pages_done >= page_count > 0`，状态 parsed 或 review）、不是被取代或未关联的修正、至少有一笔买卖、发现时间晚于开启时刻和此人加入时间，且报告日期不早于开启前 7 天（挡住首次采集时找到的旧报告）；已关联的修正标“修正”。
- 规则提醒只看 SEC 公开市场买入 P（非修正申报），SEC 接收于最近 3 天且晚于开启时刻：默认只看标普 500、纳指 100 成分（按 CIK 或代码）和自选股代码，可改为全部公司；默认排除计划交易。CEO 或 CFO 同一份申报的买入合计（`overview._priced` 的计入金额，价格不符的不计）达到门槛（默认 10 万美元）时一条（`rule_executive:<accession>`）；多人买入用总览的 `_rows` 与 `clusters`（默认 7 天 3 人），窗口里有新接收的行才算，同一家公司在 D 天内最多一条（`rule_cluster:<issuer>:<窗口首日>`）。修正申报不计入规则。
- 推送：手机推送打开时新通知的 `push_state` 为 pending，扫描后有待发消息就排一个 `notification_push` 任务（独立通道 `notify`，并发 1）。任务把待发测试放最前，其余按创建的分钟分组，同一分钟超过 5 条合并为一条摘要；两条之间至少 5 秒，24 小时最多 200 条（超出时等一小时再看）；网络请求在事务外，结果在任务租约围栏内写入。429 按 Retry-After 等待（没写时 60 秒），不算失败；其他失败按 30 秒、1、2、4、8 分钟退避，连续第 6 次失败把这条标为 failed 并继续下一条。推送文字按设置的语言由 `messages.saved_name` 从 translation.json 生成，JSON 发布到服务器根（`topic`、`title`、`message`、`click`），只含公开事实和原件公网链接。测试注入假的发送函数，不联网。
- 前端：顶栏铃铛（未读数，弹层列出最近 30 条，点开进入交易、人物或公司页，关闭时把看到的标为已读）；桌面通知是每个浏览器自己的选择（localStorage），只在有页面开着时由铃铛的轮询触发，`tag` 用通知 id，多个标签页只显示一次，首次打开不补发旧通知；设置“通知”页签有总开关、规则门槛与范围、桌面开关、ntfy 服务器 / 频道（复制、重新生成）/ 推送语言 / 订阅步骤 / 测试与最近状态。
