# 安装说明（Docker Compose）

快速开始见 [README](../README.zh-CN.md)。本文补充细节。

## 环境

需要 Docker：Linux 用 Docker Engine 与 Compose v2（支持 `up --wait`），macOS 用 Docker Desktop（Intel 与 Apple 芯片均可，镜像为多架构），Windows 在 WSL2 中运行（项目放在 Linux 文件系统里）；宿主另需 Python 3（`./iirp` 入口只用标准库）。运行镜像锁定 Python 3.13.15、Node 24.16.0、PostgreSQL 18.6，镜像摘要在 `Dockerfile`、`compose.yaml`、`image-lock.json`；Python 和前端依赖按 `uv.lock`、`package-lock.json` 安装。所有服务、测试和开发工具都在容器里运行，本机不需要安装 PostgreSQL、Node 或 Python 依赖。

## 第一次启动

```sh
./iirp start
```

- 没有 `deploy/.env` 时，`./iirp start` 从 `deploy/.env.example` 生成它：权限 600，`IIRP_DB_PASSWORD` 为随机值（不打印），`IIRP_SEC_USER_AGENT` 留空。如果该项目的数据库卷已经存在而配置文件丢失，它拒绝生成新密码（新密码与卷里的库不匹配），请找回原文件。
- 随后构建镜像、启动 PostgreSQL，等它健康后 web 执行 `alembic upgrade head` 并启动，web 健康后启动 worker。
- 打开 <http://127.0.0.1:18081>，按首页提示填写 SEC 联系信息（名字和真实邮箱）。也可以写在 `deploy/.env` 的 `IIRP_SEC_USER_AGENT` 后 `./iirp restart`；配置文件优先。规则见[运行手册](../docs/RUNBOOK.zh-CN.md)的“SEC 联系信息”。
- 全新数据库中 SEC 申报与首页行情两项自动更新默认开启（SEC 在填写联系信息后才真正运行），备份与清理默认关闭；都可在“数据与任务 → 自动更新”里开关。

`deploy/.env` 含数据库密码，已被 `.gitignore` 忽略，不要提交或粘贴到日志、Issue 中。依赖首次安装需访问官方镜像、PyPI、npm 和 PGDG。Python 依赖层在复制后端代码之前安装，只改代码时直接复用；`uv sync` 与 `npm ci` 使用 BuildKit 缓存挂载。基础镜像按摘要固定，apt 索引及 PGDG 客户端小版本没有快照锁定，不宣称字节级可重建。

## 数据卷

数据卷由项目名前缀隔离：`iirp2_postgres-data` 是数据库；`iirp2_app-runtime` 包含申报原文、备份、维护日志。容器内 `/app/runtime` 不等于宿主项目的 `runtime/`。`./iirp stop` 停服务并保留卷；不要用 `docker compose down --volumes` 停日常实例，那会删除全部数据。

## 独立实例

要在同一台机器上另起一个互不影响的实例（例如全新克隆的安装验证），同时指定 `IIRP_COMPOSE_PROJECT`、`IIRP_COMPOSE_ENV_FILE`、`IIRP_HTTP_PORT`（不能是 18081）和 `IIRP_DB_NAME`（`iirp_v1_test_*`）；缺任何一项入口都会拒绝，避免误用默认实例的配置、端口或数据库。`IIRP_COMPOSE_ENV_FILE` 指向的文件不存在时，`./iirp start` 同样从模板生成。独立实例的镜像标签由入口自动计算为 `<项目名>-app:local`。

```sh
IIRP_COMPOSE_PROJECT=iirpdemo IIRP_COMPOSE_ENV_FILE=../iirpdemo.env \
IIRP_HTTP_PORT=18091 IIRP_DB_NAME=iirp_v1_test_demo ./iirp start
```

页面只绑定本机回环地址。需要从别的电脑访问时用 SSH 端口转发，不要直接开放到公网或局域网。
