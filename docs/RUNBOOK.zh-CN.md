# 运行与使用手册

安装与配置见 [安装说明](../deploy/README.zh-CN.md)，系统结构见 [架构说明](ARCHITECTURE.zh-CN.md)。默认 `./iirp start/stop` 作用于既有的 `iirp2` 实例；另起独立实例见安装说明的“独立实例”。

## 日常操作

```sh
./iirp start
./iirp status
./iirp stop
./iirp check
./iirp test tests/sec/test_sec_poll.py
./iirp lint
./iirp frontend npm test
./iirp frontend npm run build
```

`./iirp check` 与 `./iirp test` 的后端测试连接一个临时 PostgreSQL 容器（tmpfs、独立网络，结束后连同网络删除），不在正式实例的数据库服务器上建 `iirp_v1_test_*` 库；`./iirp python` 仍在正式项目的网络中运行，不要用它跑测试。浏览器关闭后 worker 继续处理；停止 worker 会停止领取并排空/停止自己的任务，未完成工作依持久状态恢复。不要杀数据库连接冒充正常停止。`stop` 保留所有命名卷；服务启动会执行迁移，升级前先按下文“升级与恢复”演练。

## 数据、分析与控制

首页上方是五个指数的行情条（开盘时段约每分钟更新，休市显示最近交易日的收盘），下方是 Insider 实时瀑布流。Insider 查询（侧栏 Insiders）按 ticker 或名字查找；本地没有时向 SEC 查询，选中后只下载该公司或该人范围内的 Form 3/4/5。公司与人员按稳定身份（CIK）打开，同名不自动合并。

分析中心包含月度、跨年区间、财报和自定义事件分析。月度与区间在左侧条件栏点“开始分析”后才取数；结论、图表、表格和导出（统计 CSV、明细 CSV、图表 PNG）读取同一份结果。同一 request_id 的重复提交返回同一份研究。

**行情是 24 小时缓存（D14）**：每只股票一次请求“所需区间 + 前 1 个月余量”（到当天为止），存入 `price_cache`/`price_cache_bar` 并记下获取和到期时间（获取后 24 小时，查看不延长）。24 小时内月度、区间、财报、事件分析和 Insider 交易前后窗口都复用它；新需求超出已缓存区间时，只为这只股票重新取一次更宽的完整区间。财报和事件分析对每只股票只取一次：[最早反应日 R − n − 1 个交易日 − 1 个月，到当天]（D23）。分析结果的到期时间取所用缓存（含基准）中最早的一个；worker 每 10 分钟删除到期的缓存和结果，并给对应研究打上过期标记，之后打开该研究会自动重新获取（按最近已完成交易日），“重新获取”按钮立即这样做。首页报价有自己的短期缓存（`market_quote_cache`），与此无关。Yahoo 返回和计算响应的来源 JSON 只保留 24 小时，SEC 发现类响应 7 天；SEC 申报原文（`filing_version` 引用的完整申报、XML 与索引页）和解析出的交易永久保存，交易 JSONB 不再复制原始 XML，需要原文时按 `source_documents` 引用读取 `/api/v1/sources/{sha256}`。

**财报与事件（S3）**：两者同一流程。页面显示提示词模板（`{{tickers}}`、`{{years}}`、`{{event}}` 由页面上的输入替换），可复制、编辑、恢复默认；修改后的模板存在 `prompt_template`，长期保存。用户把模板拿去其他 AI 平台，把返回的简短 JSON 贴回页面：`{"events":[{"ticker","date","session","name","fiscal_year","fiscal_quarter","note"}]}`，`session` 为 `before_open`/`during`/`after_close`/`unknown`，财年财季只在财报时必填，`note` 可选；只接受这种格式。粘贴后自动校验，错误逐条写明第几条、哪个字段、为什么；预览显示每条的反应日 R。“保存并开始分析”把列表存为事件集（`event_set`，长期保存，可删除）并创建分析：每只股票一个范围，身份核对后按 D23 只取一次价格，缓存就绪即由规划器计算并发布该股票的结果，不经计算任务。窗口：盘前/盘中/未知 → R 为当天（休市顺延），盘后 → R 为下一交易日；事前 n 日 = C(R−1)/C(R−1−n) − 1，反应日 = C(R)/C(R−1) − 1，开盘跳空 = O(R)/C(R−1) − 1，事后 n 日 = C(R+n)/C(R) − 1；未到的交易日留空并写预计日期。每个窗口给出 N、中位、四分位、上涨比例，财报另按 Q1–Q4 分组。n 的默认值（财报 5、事件 5）在 `config/analysis-defaults.toml`，页面上可改。分析冻结所用事件与 n，事件集修改或删除后已有分析照常可读。

一个用户需求对应一个批次，共享计算可以服务多个批次。暂停本批次不恢复其他已暂停批次；共享任务有其他活跃订阅时仍可继续计算。取消保留已保存事实；“继续剩余”创建关联批次，旧记录保持取消终态；重试失败复用已保存数据并保持幂等。

自动更新策略在全新数据库中的默认状态：SEC 申报和首页行情报价开启（首次启动后立即调度），备份、维护关闭；行情日线没有定时更新，只按需获取；可在“数据与任务 → 自动更新”按来源开关。

**SEC 联系信息（D18、D24）**：SEC 要求自动访问在 User-Agent 中写明名字和真实邮箱。两种填写方式：

- 在界面填写（推荐）：未配置时首页顶部有提示，点击后填写名字和邮箱；之后在“数据与任务 → 自动更新”旁可查看和修改。保存在本机数据库（`user_preferences`），保存后数秒内生效，不需要重启。
- 写在 `deploy/.env` 的 `IIRP_SEC_USER_AGENT`（例如 `Jane Doe jane.doe@your-mail.com`），改后 `./iirp restart`。它优先于界面设置，此时界面显示“由配置文件设定”且不可修改。

没有真实邮箱时（留空、旧模板值 `IIRP contact@example.invalid`、RFC 2606 保留域名 `example.com/.org/.net`、`*.test`、`*.invalid`、`*.localhost`、`*.example`），SEC 策略仍显示开启，但调度不创建 SEC 任务、手动 SEC 采集返回 409、worker 不发请求；首页提示、数据页健康检查和系统状态都会说明原因。所有 SEC 请求（各 worker 通道、诊断探测）共用 `source_budget` 中的同一时隙：时隙间隔为 `(1 + 2×0.025) / sec_requests_per_second` 秒，请求必须在时隙后 25 毫秒内发出，否则重新预约，因此任意 1 秒内最多 `sec_requests_per_second`（默认 2）次。行情不受此项影响。正常目标读取 `config/collection-defaults.toml`；开发合成样本、抽样日志或已有申报条数都不等于完整覆盖。

## 备份与故障

```sh
./iirp backup
# 上一命令打印容器内备份目录，传回原样路径；这里的占位符需替换。
./iirp restore-verify /app/runtime/backups/REPLACE_WITH_BACKUP_NAME
```

每次备份成功后只保留最近 2 份完整备份，更早的备份目录和不再被引用的对象池文件随即删除（备份之间以硬链接共享原文，删除后空间随之释放）；没有完整清单的目录不会被当作备份删除。恢复验证创建随机新库和新目录，核对完整快照后关闭副本采集、暂停未完成任务、清除旧租约。它不是直接覆盖主库或切换运行库。副本需要独立空间（验证期间）；验证通过后自动删除副本原文和恢复库，只留验证报告（备份清单的 `restore_verification`，以及 `runtime/restores/<名称>-report.json`），失败时按操作记录回滚。备份不完整、目标冲突、锁超时或权限失败时保留日志，按下文“升级与恢复”处理，不能修改版本标记强行通过。

API 前端类型从 `docs/openapi.json` 生成；`./iirp check` 包含 Python 检查/真实 PG 测试、OpenAPI一致性、前端行为/类型/构建。离线缓存补充测试不等于此完整入口通过。大型容量基准用 `./iirp test -m slow`。

## 升级与恢复

只对明确选定的实例操作。应用代码回滚不等于数据库回滚；本项目不承诺任意 Alembic downgrade，已提交的结构变化只能按实测迁移路径和已验证备份处理。

迁移和恢复的要点：

- 迁移测试从空库升到旧版本，按当时结构写入合法的冻结结果、来源标识和已暂停控制，再升级到最新，核对零/未知、原 JSON 和控制版本；不通过修改 `alembic_version` 冒充旧结构。另用表锁阻断迁移，核对失败后仍停在旧版本、没有半迁移字段。
- 备份使用一致快照、数据库 dump 和来源对象哈希清单。恢复核对冻结 JSON/CSV、全部分页、来源文件、表内容指纹、控制状态和旧租约拒绝，再重启目标 PG 复核。恢复副本的自动策略关闭，未完成任务和批次暂停，避免自动领取旧任务。
- 恢复只接受本实例 `runtime/backups` 下完整、无符号链接的备份。缺失 dump、哈希不符、不支持的格式或目标已存在都直接失败；目标目录须由本次操作成功创建后才由失败清理接管。恢复用的随机库不覆盖运行库；来自不受信任来源的 dump 不得导入。
- 备份暂存、回滚和验证后丢弃共用所有权核验：创建意图先记为“待确认”，成功后记录目录设备/inode 或数据库 OID。确认前中断或目标已被替换时，保留目标并报告 `CLEANUP_FAILED`，要求人工核对。
- 运行前核对：PostgreSQL 大版本与客户端匹配、Alembic 版本、备份格式和 `app_version`、空间（源快照、目标副本和临时空间同时占用）、完整对象清单、副本的供应商执行已停止。旧客户端不保证能恢复新版 PG 的 dump；遇到未知迁移或备份格式先停下定位。

对已有实例升级的顺序：

1. 记录实际项目名、端口、迁移版本和运行策略；不打印口令。
2. 构建前先给旧镜像另打标签（例如 `docker tag iirp-v1-app:0.1.0 iirp-v1-app:pre-升级名`），作为代码回退点；Docker 使用 containerd 镜像存储时，标签移到新镜像后未命名的旧镜像会被删除。
3. `./iirp backup`（或至少只读 `pg_dump -Fc` 导出数据库）。有结构迁移时，先在恢复副本（见下文“演练副本”）上演练迁移和启动，记下耗时。
4. 停 worker（正常停止，不杀连接），用新镜像的一次性容器执行迁移，再 `./iirp start`；启动后逐页打开确认。
5. 失败时停止写入，保留证据，用回退镜像和备份恢复到另一目标核对后再决定回切。

### 迁移基线（2026-10）

2026-10 起，原来的 31 个逐步迁移（0001–0031，仍在 git 历史中）合并为一个基线 `0001_baseline`（`migrations/versions/0001_baseline.sql`，从空库执行后与原 0031 的结构逐字一致）。全新安装直接从基线建库。**在合并之前建的实例**（`alembic_version` 为 `0031`）结构已经相同，只需改版本标记，不改结构：

```sh
./iirp backup
./iirp build
docker compose -p iirp2 --env-file deploy/.env -f deploy/compose.yaml stop worker web
docker compose -p iirp2 --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps web python -m alembic stamp --purge 0001_baseline
./iirp start
```

`--purge` 先清空版本表再写入新标记（旧标记 `0031` 已不在迁移目录中，普通 `stamp` 会报找不到）。版本低于 `0031` 的实例要先用合并前的代码（git 历史中 S7 之前的提交）升级到 `0031`，再按上面执行。不要对版本不是 `0031` 的库直接打标记。

### 读取性能基准与演练副本

`scripts/bench_reads.py` 只发 GET、串行、每个接口最多 20 次，测首页、信息流首屏与第 2/3 页（全部、买入、卖出）、任务列表、公司/人员历史和交易详情的 p50、p95（nearest-rank）与字节，结果写成 JSON（放在仓库外）：

```sh
python3 scripts/bench_reads.py --base-url http://127.0.0.1:18081 --runs 10 --output ../bench/live.json
python3 scripts/bench_reads.py --base-url http://127.0.0.1:18092 --ids-from ../bench/live.json --output ../bench/drill.json
```

在恢复副本上演练（项目名、端口、库名必须与 `iirp2` 不同）：

1. `./iirp backup` 备份，或在其失败时（见下）用只读 `pg_dump -Fc` 导出数据库；恢复到独立项目（例如 `IIRP_COMPOSE_PROJECT=iirpbench`、`IIRP_HTTP_PORT=18092`、`IIRP_DB_NAME=iirp_v1_test_bench`，独立的 `IIRP_COMPOSE_ENV_FILE`，其中 `IIRP_SEC_USER_AGENT` 留空）。
2. **启动前**在副本库中关闭全部采集策略、暂停未完成任务和批次（与 `restore-verify` 相同的处理），并且只启动 `postgres` 与 `web`（`docker compose … up -d web`），**不启动 worker**，副本不得访问 SEC 或行情来源。
3. 在副本上跑 `./iirp check` 时把 `IIRP_DB_NAME` 设成一个不存在的 `iirp_v1_test_*` 名字：测试只在自建库中运行，误用配置库也只会连接失败，不会清空副本。

耗时参考（机械硬盘，数据库约 1.1GB、原文约 11 万个/1.4GB）：`./iirp backup` 约 5 分钟，`./iirp restore-verify` 约 8 分钟；恢复验证需要的空闲空间约为 2×数据库 + 全部原文 + 10GB 保留。验证耗时可能超过终端会话，可在 web 容器内分离运行：`docker exec -d iirp2-web-1 sh -c 'python scripts/backup.py verify 备份目录 > /app/runtime/logs/restore-verify.log 2>&1'`。

## 验证

`./iirp check` 是日常入口：Ruff、后端测试（临时 PostgreSQL 容器，含界面文字检查 `scripts/check_ui_text.py`）、OpenAPI 一致性、前端测试与构建。CI（`.github/workflows/ci.yml`）在每个 PR 上跑同样的检查，另有 slow 作业（大型容量与基准，`pytest -m slow`）：可手动触发；每周一定时触发，但仓库为私有时自动跳过，公开后才实际运行。本地命令成功不代表 GitHub 上的 CI 成功。性能改动用 `scripts/bench_reads.py` 做一次前后对比（见上文）。
