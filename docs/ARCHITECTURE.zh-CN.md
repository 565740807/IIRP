# 架构说明

本文简述 IIRP 的模块、数据流、任务模型和存储，帮助读者快速定位代码。产品范围和口径见 [产品与实施规划](PRODUCT_AND_IMPLEMENTATION_PLAN.zh-CN.md)，决策记录见 [DECISIONS.md](DECISIONS.md)，接口以 [openapi.json](openapi.json) 为准。

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
migrations/      Alembic 迁移（versions/00xx_*.py）
config/          采集、分析默认值与来源契约
deploy/          Dockerfile、compose.yaml、环境变量示例
scripts/         ./iirp 的实现（cli.py）、备份与恢复验证、OpenAPI 导出与检查、读取基准
tests/           pytest，目录与后端包一一对应
docs/            本文、决策、计划、运行手册、openapi.json
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
| `api/` | FastAPI 应用（`app.py`）、各领域路由（`research.py`、`events.py`）、本地只读投影（`reads.py`）和唯一的请求/响应模型（`schemas.py`）；`docs/openapi.json` 由它导出。 |
| `jobs/` | 持久任务与租约（`queue.py`、`ownership.py`）、用户需求批次（`batches.py`、`batch_views.py`）、规划（`planner.py`）、执行（`worker.py`、`handlers.py`、`operations.py`、`operation_pool.py`）、自动更新与定时（`auto_update.py`、`schedule.py`）。 |
| `sec/` | SEC EDGAR：URL 校验与解析（`parse.py`、`ownership.py`）、限速下载（`fetch.py`）、历史范围规划（`planning.py`）、最新申报轮询（`poll.py`）。 |
| `insider/` | 把申报写成按行的交易事实（`facts.py`），信息流的修订、水位线与阅读会话（`views.py`、`feed_index.py`、`feed.py`、`feed_updates.py`），公司/人员历史与交易详情（`entities.py`、`transactions.py`、`records.py`）。 |
| `market/` | Yahoo 适配器与日线校验（`yahoo.py`）、24 小时日线缓存（`cache.py`）、首页报价（`quotes.py`）、本地行情读取（`reads.py`）。 |
| `analysis/` | 所有金融计算：交易日历、月度与区间研究（`research.py`）、分布统计（`distributions.py`）、事件窗口（`event_windows.py`）、交易前后窗口；分析请求、结果复用与过期后重新获取。前端不重复实现。 |
| `events/` | 财报与自定义事件：提示词模板（`prompts.py`）、简短 JSON 校验（`input.py`）、事件集与分析（`service.py`）。 |
| `storage/` | 按内容哈希保存来源文件（`objects.py`）、维护与备份调度（`maintenance.py`）、存储盘点与系统状态。 |
| `models/` | 全部表定义，按领域分文件（`insider.py`、`market.py`、`jobs.py`、`analysis.py`、`sources.py`）；统一从 `iirp.models` 导入。 |
| `messages.py` | 返回给用户的文字是“消息代码 + 参数”，见下文“界面文字”。 |

迁移在 `migrations/`（Alembic）。采集与保留的默认值在 `config/collection-defaults.toml`，分析的 n 默认值在 `config/analysis-defaults.toml`，来源契约在 `config/provider-contracts.json`。

## 前端（`frontend/src`）

页面在 `pages/`（首页、Insider、分析、事件、数据与任务），共享组件在 `components/`。服务器状态用 TanStack Query，图表用 ECharts。接口类型由 `docs/openapi.json` 生成到 `generated/api.ts`（`npm run generate:api`），`npm run check:api` 校验两者一致。前端只展示后端计算好的结果。

### 界面文字

后端不返回某种语言的成句文字。报错、状态说明、提示、任务标题和方法说明都是消息 `{"code": "...", "params": {...}}`（`iirp.messages.msg`）；放在原来的文本字段（数据库列、异常、子进程结果、JSON 响应）里时是该对象的紧凑 JSON 字符串，HTTP 报错的 `detail` 直接是对象。前端用 [i18next](https://www.i18next.com/) / react-i18next 按代码显示：翻译在 `frontend/src/locales/<语言>/translation.json`（i18next JSON 格式，`{{参数}}` 插值），`api()` 收到响应时把其中的消息换成当前语言的文字（`src/i18n.ts` 的 `localize`）。消息出现之前写入的旧文字原样显示。`tests/test_messages.py` 检查后端用到的每个代码都有翻译。目前只有中文；前端页面自身的文字仍写在组件里，英文界面与切换在 S5 完成。

## 数据流

1. **采集**：用户在界面提交需求（或启用自动策略）→ API 写入批次与任务 → worker 领取任务 → 访问外部来源（网络调用不在数据库事务内）→ 原文按内容哈希存入 `runtime/objects`，解析结果在一次带围栏的事务中提交为事实。
2. **SEC 内部人交易**：最新申报由 worker 按来源轮询（`iirp/sec/poll.py`，状态是 `source_poll` 中每个来源一行：水位线、未读完的补扫游标、下次时间、租约），不为每次轮询建任务；历史回补用 `sec_discover` 扫日/季索引 → `sec_document` 下载并解析 Form 3/4/5 → 写入申报、版本、申报人和按行的交易事实；修订按行记录，不覆盖旧版本。交易日、接受时间和发现时间分开保存；缺失、未知和已知零分别表示。
3. **行情**：`market_identity` 确认证券身份 → 规划时若该证券的缓存覆盖不了所需区间，`market_history` 一次取“所需区间 + 前 1 个月余量”到当天，替换为一份新的 24 小时缓存（`price_cache` + `price_cache_bar`，仅拆股调整的 OHLC、分红与拆股，记下获取与到期时间）→ `market_quote` 另行更新首页报价。到期的缓存由 worker 删除。
4. **分析**：用户显式应用条件 → 月度/区间由 `research_compute` 任务、财报/事件由规划器在行情缓存就绪后直接读取缓存日线，计算并写入结果（记下所用缓存与获取时间）。结果与所用缓存同生命周期（`analysis_result.expires_at`），图表、表格、摘要和 JSON/CSV/PNG 导出都读同一份结果；过期后打开研究会自动按最近已完成交易日重新获取。
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

## 后续改造

尚未完成的界面与展示工作（英文界面、首页与 Insider、分析结果展示）见 [改进计划](IMPROVEMENT_PLAN.zh-CN.md)。
