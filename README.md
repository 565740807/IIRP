# IIRP

IIRP 是一个在本机运行的美股投资研究工具，面向电脑浏览器。它把 SEC 内部人交易申报和行情日线整理成可核对的本地数据，并在此基础上做月度、跨年区间、财报和自定义事件分析。

> **状态：开发中，尚未完成 V1。** 现有功能已能运行，但历史数据覆盖、性能和界面仍在改进，见[改进计划](docs/IMPROVEMENT_PLAN.zh-CN.md)。开发时用的小样数据不代表完整覆盖。

![首页](docs/images/home.png)

*当前首页（开发中）。截图来自本机实例：行情卡片显示的是最近一次成功获取的数据日期，涨跌颜色将在后续改为色弱友好的蓝/橙。*

## 主要功能

- **Insider 信息流**：采集 SEC Form 3/4/5，按公司和日期分组；交易日、接受时间和发现时间分开保存，修订按行记录，缺失、未知和已知零分别展示。
- **公司与人员详情**：按稳定身份查看历史，同名不自动合并，交易详情可回到 SEC 原文。
- **行情**：指数、期货和个股日线（仅做拆股调整）。日线是 24 小时缓存：每只股票按所需区间（另加 1 个月余量）一次取完，24 小时内所有分析和交易前后窗口都复用，到期自动删除，再次需要时重新获取；首页报价另有短期缓存。
- **分析中心**：月度分析、跨年区间（条件显式应用，结果可导出 JSON/CSV/PNG），以及财报与自定义事件分析：页面提供提示词模板（可修改、恢复默认），拿去其他 AI 平台问到日期后把简短 JSON 贴回来，按日期取价，计算事前 n 日、反应日（含开盘跳空）和事后 n 日的涨跌，财报另按财季统计。结果与所用行情缓存同生命周期：24 小时内重复打开不再下载，过期后打开会自动重新获取。
- **任务与采集控制**：SEC 采集、回补和行情获取都是持久任务，可暂停、恢复、取消、重试；浏览器关闭后 worker 继续工作。SEC 申报原文与交易数据永久保存。
- **备份与恢复**：一致快照备份（只保留最近 2 份），并可在随机新库上验证恢复。

金融计算只在后端完成，前端只展示结果。

## 快速开始（Docker Compose）

需要 Docker（Linux 用 Docker Engine 与 Compose v2，macOS 用 Docker Desktop；Windows 请在 WSL2 中运行）和 Python 3（`./iirp` 入口只用标准库）。所有服务、测试和开发工具都在容器里运行，本机不需要安装 PostgreSQL、Node 或 Python 依赖。详细说明见[安装说明](deploy/README.zh-CN.md)。

```sh
git clone <本仓库地址> iirp && cd iirp
install -m 600 deploy/.env.example deploy/.env
# 编辑 deploy/.env：填写随机的 IIRP_DB_PASSWORD，以及含真实联系邮箱的 IIRP_SEC_USER_AGENT（未配置时 SEC 不运行）
./iirp build
./iirp start
```

启动后访问 <http://127.0.0.1:18081>（仅监听本机回环地址）。常用命令：

```sh
./iirp status   # 查看服务状态
./iirp check    # Ruff、后端测试（临时 PostgreSQL 容器）、OpenAPI 一致性、前端测试与构建
./iirp stop     # 停止服务，保留数据卷
```

`deploy/.env` 含私有配置，已被 `.gitignore` 忽略，不要提交。

> **首次启动会立即开始自动更新。** 全新数据库中，SEC 申报和首页行情报价两项自动更新策略默认开启（备份、维护默认关闭），首页打开时即开始请求报价。**SEC 只有在 `IIRP_SEC_USER_AGENT` 含真实联系邮箱时才会自动运行**：留空、保留模板值（`IIRP contact@example.invalid`）或使用 RFC 2606 保留域名（`example.com/.org/.net`、`*.test`、`*.invalid`、`*.localhost`、`*.example`）时，不向 SEC 发任何请求，页面顶部的更新状态、“数据与任务”的自动更新开关和系统状态面板（以及 `/api/v1/system` 的 `sec_user_agent`）会显示“需配置 SEC User-Agent”。填好后执行 `./iirp restart` 生效。所有 SEC 请求共用一个全局限速（`config/collection-defaults.toml` 的 `sec_requests_per_second`，默认任意 1 秒内最多 2 次）。可在“数据与任务”页按来源关闭自动更新。默认的采集与回补范围、行情缓存时长在 [`config/collection-defaults.toml`](config/collection-defaults.toml)。

## 数据来源与使用条款

- **SEC EDGAR**：内部人交易申报来自 SEC 公开数据。SEC 要求自动化访问在 User-Agent 中提供**真实的联系邮箱**（`IIRP_SEC_USER_AGENT`），并遵守其访问频率限制；使用前请阅读 [SEC 的访问说明](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)。
- **行情**：日线和报价通过开源库 [yfinance](https://github.com/ranaroussi/yfinance) 获取。yfinance **不是 Yahoo 的官方产品**，只是访问 Yahoo Finance 公开接口的第三方工具，数据使用受 Yahoo 的服务条款约束，仅供个人研究和学习，数据可能延迟、缺失或被更正。行情适配器是可替换的。
- 本项目不提供投资建议，也不对数据的准确性、完整性或及时性作保证。

## 文档

| 文档 | 内容 |
|---|---|
| [产品与实施规划](docs/PRODUCT_AND_IMPLEMENTATION_PLAN.zh-CN.md) | 产品范围、口径、技术方案 |
| [架构说明](docs/ARCHITECTURE.zh-CN.md) | 模块、数据流、任务模型、存储 |
| [决策记录](docs/DECISIONS.md) | 已做出的产品与技术决策 |
| [运行手册](docs/RUNBOOK.zh-CN.md) | 日常操作、备份与恢复、升级、验证 |
| [安装说明](deploy/README.zh-CN.md) | Compose 部署与配置 |
| [接口约定](docs/API_CONTRACT.md) 与 [OpenAPI](docs/openapi.json) | 后端接口 |
| [改进计划](docs/IMPROVEMENT_PLAN.zh-CN.md) | 当前问题、阶段计划与进度 |
| [第三方声明](THIRD_PARTY_NOTICES.md) | 依赖清单与许可材料 |
| [AGENTS.md](AGENTS.md) | 开发约定（含 AI 编码助手使用的说明） |

## 许可

本项目代码以 [MIT 许可证](LICENSE) 发布。第三方依赖各自保留其许可证，见[第三方声明](THIRD_PARTY_NOTICES.md)。
