# 架构说明

本文简述 IIRP 的模块、数据流、任务模型和存储，帮助读者快速定位代码。产品范围和口径见 [产品与实施规划](PRODUCT_AND_IMPLEMENTATION_PLAN.zh-CN.md)，决策记录见 [DECISIONS.md](DECISIONS.md)，接口以 [openapi.json](openapi.json) 为准。

## 组成

```
浏览器（React + TypeScript）
   │  同源 JSON，/api/v1
   ▼
web：FastAPI（backend/iirp/api.py 等 *_api.py）  ── 只读本地数据；写操作只创建持久任务
   │
   ▼
PostgreSQL 18  ◄── worker：python -m iirp.worker（领取任务、调用外部来源、提交事实）
   │                         │
   │                         ▼
   │                  SEC EDGAR / yfinance / 用户导入的 CSV
   ▼
runtime/ 卷：来源原文（按内容哈希存放）、备份、维护日志
```

Compose 启动三个服务：`postgres`、`web`（启动时先执行 `alembic upgrade head`）、`worker`。web 和 worker 使用同一个镜像。API 从不在请求中等待外部来源；浏览器关闭后 worker 继续工作。

## 后端模块（`backend/iirp`）

| 领域 | 主要模块 | 说明 |
|---|---|---|
| 接口 | `api.py`、`research_api.py`、`event_api.py`、`contracts.py`、`event_contracts.py` | 路由与请求/响应模型；`docs/openapi.json` 由应用导出 |
| 任务与调度 | `queue.py`、`worker.py`、`business_worker.py`、`worker_ownership.py`、`operations.py`、`operation_pool.py`、`shared_compute.py`、`lifecycle.py` | 持久任务、租约与围栏、批次与订阅、共享计算 |
| SEC 内部人交易 | `sec_sources.py`、`sec_facts.py`、`feed_snapshots.py`、`feed_updates.py`、`entity_reads.py` | 发现与下载申报、解析为按行事实、信息流与公司/人员详情 |
| 行情 | `market_data.py`、`market_quotes.py`、`market_http.py`、`market_ranges.py`、`ticker_identity.py`、`freshness.py` | yfinance 适配器、版本化日线、报价、证券身份 |
| 财报与事件 | `earnings_data.py`、`earnings_planner.py`、`event_service.py`、`event_pipeline.py`、`event_views.py`、`imports.py` | 财报候选与核对、自定义事件集、CSV 预览与导入 |
| 研究计算 | `analytics/`（`research.py`、`prices.py`、`calendar.py`、`distributions.py`、`event_dates.py`、`event_overlaps.py`、`fiscal_calendar.py`）、`research_pipeline.py`、`result_storage.py`、`result_reuse.py` | 月度、跨年区间、财报与事件分析；所有金融计算都在这里，前端不重复实现 |
| 维护 | `maintenance.py`、`storage.py`、`storage_inventory.py`、`capacity.py`、`system_status.py` | 保留策略、清理、备份、容量与系统状态 |
| 基础 | `config.py`、`db.py`、`models.py`、`business_models.py`、`event_models.py`、`profiles.py`、`providers.py` | 配置、会话、表定义、来源预算 |

迁移在 `migrations/`（Alembic）。采集与保留的默认值在 `config/collection-defaults.toml`，来源契约在 `config/provider-contracts.json`。

## 前端（`frontend/src`）

页面在 `pages/`（首页、Insider、分析、事件、数据与任务），共享组件在 `components/`。服务器状态用 TanStack Query，图表用 ECharts。接口类型由 `docs/openapi.json` 生成到 `generated/api.ts`（`npm run generate:api`），`npm run check:api` 校验两者一致。前端只展示后端计算好的结果。

## 数据流

1. **采集**：用户在界面提交需求（或启用自动策略）→ API 写入批次与任务 → worker 领取任务 → 访问外部来源（网络调用不在数据库事务内）→ 原文按内容哈希存入 `runtime/objects`，解析结果在一次带围栏的事务中提交为事实。
2. **SEC 内部人交易**：`sec_discover` 发现最新申报和日索引 → `sec_document` 下载并解析 Form 3/4/5 → 写入申报、版本、申报人和按行的交易事实；修订按行记录，不覆盖旧版本。交易日、接受时间和发现时间分开保存；缺失、未知和已知零分别表示。
3. **行情**：`market_identity` 确认证券身份 → `market_history` 一次取完整起止区间，保存仅拆股调整的 OHLC、分红与拆股，并记录覆盖与版本 → `market_quote` 更新报价。
4. **分析**：用户显式应用条件 → `research_compute`/`event_compute` 读取本地事实与日线，计算并写入冻结结果（带数据版本与来源标识）。图表、表格、摘要和 JSON/CSV/PNG 导出都读同一个冻结结果；重开旧结果不会自动换成最新条件。
5. **读取**：所有 GET 只读本地数据库；信息流和详情在阅读会话中保持稳定，新内容以提示形式出现。

## 任务模型

- **持久任务**（`job`）：状态为 `QUEUED`、`RUNNING`、`PAUSE_REQUESTED`、`PAUSED`、`CANCEL_REQUESTED`、`CANCELLED`、`FAILED`、`RETRY_WAIT`、`PARTIAL`、`SUCCEEDED` 等；用户的暂停、恢复、取消、重试写为持久意图，由 worker 在安全点执行。
- **租约与围栏**：worker 领取任务时拿到租约令牌并持续心跳；每次提交事实都带令牌校验，租约丢失（`OwnershipLost`）则整笔事务放弃，过期租约由调度回收后重新排队。执行是至少一次，因此提交必须幂等。
- **批次与订阅**：一个用户需求对应一个批次（`batch`）；批次通过 `batch_job` 订阅任务。多个批次可以共享同一个计算任务（`shared_compute`），暂停或取消某个批次只影响它自己的订阅，仍有其他活跃订阅时共享任务继续运行。
- **依赖**：`job_dependency` 表达先后关系，前置任务未成功前后续任务不会被领取。
- **来源预算与限流**：`source_budget` 记录各来源的请求预算；所有外部请求走同一个传输入口，由调用方统一限速。
- **主要任务类型**：`sec_probe`、`sec_discover`、`sec_identity`、`sec_document`、`market_identity`、`market_history`、`market_quote`、`earnings_candidates`、`earnings_evidence`、`local_import`、`research_compute`、`event_compute`、`maintenance_backup`、`maintenance_clean`。

## 存储

- **PostgreSQL**：业务事实（`issuer`、`reporting_owner`、`security`、`filing`、`filing_version`、`transaction_event`、`price_dataset_version`、`market_bar_revision`、`dataset_bar`、`earnings_event` 等）、任务与批次（`job`、`batch`、`batch_job`）、冻结结果（`analysis_request`、`analysis_result`、`export_manifest`）、事件集（`event_set`、`event_set_version`）、维护记录。
- **来源对象**：原始 XML/JSON/CSV 以内容哈希为文件名存放在 `runtime/objects/<前两位>/<哈希>`，数据库的 `source_object` 与 `source_observation` 记录来源、哈希和观察时间。
- **卷**：Compose 的 `postgres-data`（数据库）与 `app-runtime`（来源对象、备份、维护日志、恢复副本）。`./iirp stop` 保留卷；备份由 `./iirp backup` 生成一致快照（`pg_dump` + 来源对象哈希清单），`./iirp restore-verify` 在随机新库上验证恢复。
- **测试**：后端测试在隔离的 `iirp_v1_test_*` 数据库中运行，不访问运行库。

## 已知的结构问题

当前的主要问题（信息流与任务接口的响应时间、数据库体积、行情过期提示、模块平铺等）和对应的改造阶段见 [改进计划](IMPROVEMENT_PLAN.zh-CN.md)。
