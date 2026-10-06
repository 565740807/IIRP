# 运行与使用手册

安装与配置见 [安装说明](../deploy/README.zh-CN.md)，系统结构见 [架构说明](ARCHITECTURE.zh-CN.md)。默认 `./iirp start/stop` 作用于既有的 `iirp2` 实例，不用于创建验证副本；验证副本见本文“验证”一节。

## 日常操作

```sh
./iirp start
./iirp status
./iirp stop
./iirp check
./iirp test tests/test_shared_compute.py
./iirp lint
./iirp frontend npm test
./iirp frontend npm run build
```

`./iirp check` 与 `./iirp test` 的后端测试连接一个临时 PostgreSQL 容器（tmpfs、独立网络，结束后连同网络删除），不在正式实例的数据库服务器上建 `iirp_v1_test_*` 库；`./iirp python` 仍在正式项目的网络中运行，不要用它跑测试。浏览器关闭后 worker 继续处理；停止 worker 会停止领取并排空/停止自己的任务，未完成工作依持久状态恢复。不要杀数据库连接冒充正常停止。`stop` 保留所有命名卷；服务启动会执行迁移，升级前先按下文“升级与恢复”演练。

## 数据、分析与控制

主页“更新市场行情”提交需求，卡片显示已保存值、来源时间和缺口；期货与指数单位不同，日线不等于实时行情。Insider 可按最新、历史范围或完整 SEC 原文 URL 提交；公司与人员按稳定身份打开。同名不能自动合并。

分析中心包含月度、跨年区间、财报和事件日期分析。修改条件后显式应用；图表、表格、摘要和 JSON/CSV/PNG 读取同一份结果。草稿、迟到创建回执和丢响应重试各有持久身份，遇到不确定回执先恢复当前命令，不反复创建需求。

**行情是 24 小时缓存（D14）**：每只股票一次请求“所需区间 + 前 1 个月余量”（到当天为止），存入 `price_cache`/`price_cache_bar` 并记下获取和到期时间（获取后 24 小时，查看不延长）。24 小时内月度、区间、财报、事件分析和 Insider 交易前后窗口都复用它；新需求超出已缓存区间时，只为这只股票重新取一次更宽的完整区间。分析结果的到期时间取所用缓存（含基准）中最早的一个；worker 每 10 分钟删除到期的缓存和结果，并给对应研究打上过期标记，之后打开该研究会自动重新获取（按最近已完成交易日），“重新获取”按钮立即这样做。首页报价有自己的短期缓存（`market_quote_cache`），与此无关。Yahoo 返回和计算响应的来源 JSON 只保留 24 小时，SEC 发现类响应 7 天；SEC 申报原文（`filing_version` 引用的完整申报、XML 与索引页）和解析出的交易永久保存，交易 JSONB 不再复制原始 XML，需要原文时按 `source_documents` 引用读取 `/api/v1/sources/{sha256}`。

财报先发现候选并核对财年/财季、公告时刻与来源。电话会议时间不代替财报公布时刻；日期级事件不进入精确价格反应。有效样本数按窗口计算。财报 CSV 预览确认后导入，只保存为候选；不接受 CSV 行情。缺失、未知和已知零分别展示。

一个用户需求对应一个批次，共享计算可以服务多个批次。暂停本批次不恢复其他已暂停批次；共享任务有其他活跃订阅时仍可继续计算。取消保留已保存事实；“继续剩余”创建关联批次，旧记录保持取消终态；重试失败复用已保存数据并保持幂等。

自动更新策略在全新数据库中的默认状态：SEC 申报和首页行情报价开启（首次启动后立即调度），财报、备份、维护关闭；行情日线没有定时更新，只按需获取；可按来源在界面开关。SEC 只有在 `IIRP_SEC_USER_AGENT` 含真实联系邮箱时才运行：留空、模板值或 RFC 2606 保留域名（`example.com/.org/.net`、`*.test`、`*.invalid`、`*.localhost`、`*.example`）一律视为未配置，此时 SEC 策略仍显示开启，但调度不创建 SEC 任务、手动 SEC 采集返回 409、worker 不发请求；更新状态、自动更新开关、系统状态面板和 `/api/v1/system` 的 `sec_user_agent` 显示“需配置 SEC User-Agent”。修改 `deploy/.env` 后 `./iirp restart`。所有 SEC 请求（各 worker 通道、财报核对、诊断探测）共用 `source_budget` 中的同一时隙：时隙间隔为 `(1 + 2×0.025) / sec_requests_per_second` 秒，请求必须在时隙后 25 毫秒内发出，否则重新预约，因此任意 1 秒内最多 `sec_requests_per_second`（默认 2）次。行情不受此项影响。正常目标读取 `config/collection-defaults.toml`；开发合成样本、抽样日志或已有申报条数都不等于完整覆盖。供应商失败、未来数据未形成和身份待核对须保留其实际状态。

## 备份与故障

```sh
./iirp backup
# 上一命令打印容器内备份目录，传回原样路径；这里的占位符需替换。
./iirp restore-verify /app/runtime/backups/REPLACE_WITH_BACKUP_NAME
```

每次备份成功后只保留最近 2 份完整备份，更早的备份目录和不再被引用的对象池文件随即删除（备份之间以硬链接共享原文，删除后空间随之释放）；没有完整清单的目录不会被当作备份删除。恢复验证创建随机新库和新目录，核对完整快照后关闭副本采集、暂停未完成任务、清除旧租约。它不是直接覆盖主库或切换运行库。副本需要独立空间（验证期间）；验证通过后自动删除副本原文和恢复库，只留验证报告（备份清单的 `restore_verification`，以及 `runtime/restores/<名称>-report.json`），失败时按操作记录回滚。备份不完整、目标冲突、锁超时或权限失败时保留日志，按下文“升级与恢复”处理，不能修改版本标记强行通过。

API 前端类型从 `docs/openapi.json` 生成；`./iirp check` 包含 Python 检查/真实 PG 测试、OpenAPI一致性、前端行为/类型/构建。离线缓存补充测试不等于此完整入口通过。慢容量和浏览器命令见下文“验证”。

## 升级与恢复

只对明确选定的实例操作。应用代码回滚不等于数据库回滚；本项目不承诺任意 Alembic downgrade，已提交的结构变化只能按实测迁移路径和已验证备份处理。

独立演练（在全新的 Compose 项目和空库上执行真实 `alembic upgrade head`，再把合成备份恢复到另一新项目/卷）：

```sh
python3 scripts/validate.py --suite recovery --output test-results/recovery
```

- 迁移测试从空库升到旧版本，按当时结构写入合法的冻结结果、来源标识和已暂停控制，再升级到最新，核对零/未知、原 JSON 和控制版本；不通过修改 `alembic_version` 冒充旧结构。另用表锁阻断迁移，核对失败后仍停在旧版本、没有半迁移字段。
- 备份使用一致快照、数据库 dump 和来源对象哈希清单。恢复核对冻结 JSON/CSV、全部分页、来源文件、表内容指纹、控制状态和旧租约拒绝，再重启目标 PG 复核。恢复副本的自动策略关闭，未完成任务和批次暂停，避免自动领取旧任务。
- 恢复只接受本实例 `runtime/backups` 下完整、无符号链接的备份。缺失 dump、哈希不符、不支持的格式或目标已存在都直接失败；目标目录须由本次操作成功创建后才由失败清理接管。恢复用的随机库不覆盖运行库；来自不受信任来源的 dump 不得导入。
- 备份暂存、回滚和验证后丢弃共用所有权核验：创建意图先记为“待确认”，成功后记录目录设备/inode 或数据库 OID。确认前中断或目标已被替换时，保留目标并报告 `CLEANUP_FAILED`，要求人工核对。
- 运行前核对：PostgreSQL 大版本与客户端匹配、Alembic 版本、备份格式和 `app_version`、空间（源快照、目标副本和临时空间同时占用）、完整对象清单、副本的供应商执行已停止。旧客户端不保证能恢复新版 PG 的 dump；遇到未知迁移或备份格式先停下定位。

对已有实例升级或切换的顺序：

1. 记录实际项目名、端口、数据库与客户端版本、迁移版本、卷、待执行批次和运行策略；不打印口令。
2. 按持久控制停止来源调度，正常停止 worker 并排空，确认租约和未完成工作；不杀连接强行迁移。保留旧配置和镜像：构建前先给旧镜像另打标签（例如 `docker tag iirp-v1-app:0.1.0 iirp-v1-app:pre-升级名`）；Docker 使用 containerd 镜像存储时，标签移到新镜像后未命名的旧镜像会被删除。
3. 创建一致快照备份，在另一实例恢复并核对指纹、来源文件、冻结结果和分页。没有恢复证明不切换。
4. 在隔离的恢复副本上执行同版本升级和完整检查。全部成功后再在维护窗口切换，先只读复核，再按原意图逐项恢复调度。
5. 失败时停止新副本写入，保留失败证据和新增事实；恢复旧库快照与匹配旧应用到另一目标，核对一致性后再决定回切。新写入如何回收须明确，不静默丢失。

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

已知问题（2026-10-04 实测）：`./iirp backup` 在机械硬盘和 768MiB 的 PostgreSQL 上，`SELECT … FROM source_object ORDER BY sha256`（约 65 万行）超过 5 分钟语句超时而失败（QueryCanceled），最近一次成功的备份停在 2026-09-28。此时可用只读 `pg_dump` 导出数据库（约 1GB、5 分钟）做演练，但它不含来源原文，不能替代完整备份。2026-10-05 升级到 PG 2GiB 并消除 worker 对 job 表的反复全表扫描（迁移 0023）后，正式实例 `./iirp backup` 58 分钟成功，该查询不到 1 分钟。`restore-verify` 需要的空闲空间约为 2×数据库 + 全部原文 + 每个文件 4KB + 10GB 保留（当时约 53GB），在机械硬盘上约 3 小时；自 S1 起验证通过后副本自动删除。验证耗时可能超过终端会话，可在 web 容器内分离运行：`docker exec -d iirp2-web-1 sh -c 'python scripts/backup.py verify 备份目录 > /app/runtime/logs/restore-verify.log 2>&1'`。

### 升级到迁移 0020–0023（P2-A）

- 按上文顺序先停止来源调度和 worker。0020 并发创建任务列表索引，不阻塞读写；0021 先提交结构变更（修订表的排他锁只持续毫秒级），再回填每组当前修订和排序键，最后并发建索引；0022 重算被异常日期带偏的排序日期。在恢复副本（约 2.1 万个分组、4.7 万条修订、32.5 万个任务）上实测：0020 约 32 秒、0021 约 41 秒、0022 约 39 秒（均含一次性容器启动，数据库部分约 10 秒）；迁移期间旧 web 继续读信息流，只有 0021 结构变更排队等待旧读请求时出现一次 500 毫秒锁超时。0023 并发创建“上一次完整 SEC 扫描”查找用的部分索引，不阻塞读写。2026-10-05 正式实例实测：停 web 与 worker 后 0014→0022 一次完成约 35 秒，0023 在 web 在线时约 7 秒（均含容器启动）。
- `deploy/compose.yaml` 中 PostgreSQL 内存上限已改为 2GiB（PGTune 参数），已有实例在下一次 `./iirp start` 时重建 PostgreSQL 容器才会生效；请在维护窗口执行。
- 升级后新的信息流会话不再写 `feed_manifest`；旧会话在 12 小时内过期，旧清单和过期会话由“维护”策略或“清理未引用缓存”按 24 小时宽限期回收（维护策略默认关闭）。维护只删除过期阅读会话和未引用清单（每次最多 25 个），研究结果一律保留；web/PG 日志轮转由 worker 每分钟独立执行，与该开关无关。

### 升级到迁移 0024（S1）

- 0024 新建 `source_poll`（SEC 最新申报的轮询状态，水位线初始化为已保存申报中最新的接受时间）、给 `source_object` 加可空的 `expires_at`（只改目录，不重写表）并并发建部分索引；把旧方案遗留的未完成 latest `sec_discover` 任务标为已取消（不删除），并让未结束的 latest 批次不再订阅它们。迁移前停 worker（停止旧的逐次轮询）；10-05 恢复副本上演练约 7 秒。
- 升级后 worker 按 D22 节奏轮询（开盘 60 秒、盘前 2 分钟、其他 30–60 分钟；开页最多 30 秒提前一次），不再生成 latest `sec_discover` 任务。历史任务和旧来源文件的清理属于 S2。

### 升级到迁移 0025（S2）

- 0025 新建 `price_cache`、`price_cache_bar`，给 `analysis_result` 加 `expires_at`（用目录内默认值，不重写已有行；旧结果随即视为过期，由 worker 删除），删除 `security.maintain`、`active_until`，清掉行情/财报策略的定时字段。旧的版本化价格表（`price_dataset_version`、`market_bar_revision`、`dataset_bar`、`corporate_action`）不再读写，`migrations/env.py` 暂不比对它们；它们的数据在确认后的清理步骤中删除。迁移只改结构和小表，先停 worker，用一次性容器执行。
- 升级后打开旧研究会自动重新获取行情（结果已过期）。

## 验证

`./iirp check` 是日常入口。独立、可重复的合成验证用：

```sh
python3 scripts/release_candidates.py --output ../iirp-export
cd ../iirp-export
python3 scripts/validate.py --suite full --output test-results/full
```

需要可用的 Docker Engine/Compose、网络和本地端口。入口创建随机的 Compose 项目、`iirp_v1_test_*` 数据库、卷、私有配置和端口，不触碰 `iirp2`；失败保留日志，结束时只清理自己创建的资源。输出目录须是新建的。`--suite check|install|browser|recovery|capacity` 可以定位失败阶段。完整验证耗时较长（GitHub 工作流 `validation.yml` 仅手动触发，上限 180 分钟），只在公开发布前整体运行一次。本地命令成功不代表 GitHub 上的 CI 成功。

**浏览器**：`browser/package-lock.json` 固定 Playwright Core 版本和 SHA-512，`browser/browser-lock.json` 固定 Chromium 修订；`deploy/Browser.Dockerfile` 把匹配的浏览器装进项目内 `.browsers`，测试拒绝使用宿主 Chrome 路径、channel 或个人缓存。合成服务提供随机身份和实际 `current_database()`，客户端先核对身份、`iirp_v1_test_*` 库名并拒绝默认实例端口。桌面矩阵为 1366×768、1440×900、1920×1080，覆盖四种研究、鼠标/Tab、窗口调整、滚动稳定、图表以及冻结 JSON/CSV/PNG。受控响应拦截只用于迟到/丢响应行为，计算、PG 和共享控制走真实路径，报告中分开描述。CDP 的 pageScaleFactor 只是视觉缩放，不算浏览器菜单缩放。

**远程浏览器客户端**：在验证主机启动合成夹具并保留 manifest、数据库身份和输出，不要转发默认实例端口；客户端用 `ssh -N -L 本地端口:127.0.0.1:夹具端口 用户@主机` 转发，先检查 `__iirp_test_identity__` 与夹具一致，再在浏览器菜单实际选择 80%/100%/125%/150% 缩放并截图；结束后关闭转发、正常停止夹具并确认自建进程与数据库已清理。

**容量与统计**：目标写在 `config/validation-targets.json`：A14、B10（1500 事件）、C100 保留完整的结果/来源/分页断言；应用与 PG 内存上限、SQL 5 秒、锁 500 毫秒、OperationPool 输入 16MiB/输出 80MiB/110 秒。真实 HTTP 客户端在宿主独立进程，对只读和后台 worker 写入各跑 100 轮，每轮校验冻结结果 SHA 与 1500 行；socket 超时 5 秒、零重试。p50 取中位数，p95 取 nearest-rank，最大值单独报告；默认/详情 p95 ≤ 500 毫秒、最大 ≤ 1000 毫秒。已知问题：17MiB 备注的容量用例在当前资源上限下仍会超时（2026-10-04 `./iirp check` 复现），归入改进计划 P2 的容量调整；新环境没有实际运行时不生成假样本。
