# Linux / Compose 安装

当前支持 Linux、Docker Engine 与 Compose v2（支持 `up --wait`）、Python3（宿主入口仅标准库）。运行镜像锁定 Python3.13.15、Node24.16.0、PostgreSQL18.6，镜像摘要在 Dockerfile / compose.yaml / image-lock.json；Python 和前端依赖按 uv.lock / package-lock.json 安装。宿主 Python3.14 不用于后端测试。不要传输宿主虚拟环境、node_modules 或旧 PostgreSQL 数据目录。

## 新日常安装

从已核对的候选进入项目目录，确认默认项目 `iirp2` 和端口18081尚未被本项目旧安装占用。配置文件已存在时停止此流程，先识别其所属实例。

```sh
install -m 600 deploy/.env.example deploy/.env
# 在编辑器中填写 URL-safe 随机 IIRP_DB_PASSWORD 及已授权的 SEC 联系信息。
# 注意：SEC 和行情自动更新在全新数据库中默认开启，行情启动后立即请求；
# SEC 只有在 IIRP_SEC_USER_AGENT 含真实联系邮箱时才运行（模板值、example.com、
# *.test/*.invalid/*.localhost 等保留域名视为未配置，界面提示“需配置 SEC User-Agent”）。
# 不把填写后的内容粘贴到日志、Issue 或版本库。
./iirp build --pull --no-cache
./iirp start
./iirp status
./iirp check
```

`deploy/.env.example` 是唯一无秘密 Compose 模板。根目录 `.env.example` 的原有删除不恢复；旧 Mac `.env` 是历史入口的私有配置，不是 Linux 模板。不要在已有文件上执行上述 install 命令。依赖和浏览器首次安装需访问官方镜像、PyPI、npm、PGDG 和 Playwright 下载服务；保留构建日志，以区分下载、层缓存与失败重试。Python 依赖层在复制后端代码之前安装，只改代码时直接复用；`uv sync` 与 `npm ci` 使用 BuildKit 缓存挂载，`uv.lock` 变化时也只下载新增的包（缓存不进入镜像，安装仍按锁文件哈希校验）。基础镜像按摘要固定，apt 索引及 PGDG 客户端小版本尚未做快照锁定，不宣称字节级可重建。

数据卷由项目名前缀隔离：`iirp2_postgres-data` 是 PG18 数据；`iirp2_app-runtime` 包含来源对象、备份、维护日志和独立恢复副本。容器内 `/app/runtime` 不等于宿主项目的 `runtime/`。`./iirp stop` 停服务并保留卷；切勿用 `down --volumes` 停日常实例。数据库、来源原文与备份必须一起规划空间和保留。

## 独立合成验收

在独立导出目录执行：

```sh
python3 scripts/validate.py --suite full --output test-results/full
```

脚本自动生成不同于 `iirp2` 的项目名、`iirp_v1_test_*` 数据库、私有0600配置、新卷和非18081回环端口。构建使用独立镜像标签及 `--pull --no-cache`。所有源码/迁移来自当前候选，首次开发工具安装不依赖旧缓存。测试身份覆盖只允许合成计算和 fixture_check，禁用外部供应商领取；产品默认 worker 不受影响。

手工隔离配置须同时指定 `IIRP_COMPOSE_PROJECT`、`IIRP_COMPOSE_ENV_FILE`、`IIRP_HTTP_PORT`、`IIRP_DB_NAME`；非默认项目拒绝缺省主库、主配置或18081。浏览器测试还须设置32位十六进制 `IIRP_VALIDATION_ID`，入口会追加 `deploy/validation.compose.yaml`。命令行环境优先于 env 文件，独立镜像标签由入口强制计算。不能直接复制省略项目参数的 `docker compose up/stop` 命令。

正式 `start` 先等待 PG 健康、迁移到head、等待网页健康，然后启动 worker。默认循环地址为 `http://127.0.0.1:18081`。外部桌面使用 SSH 转发，保持回环绑定和 Host/Origin 边界，不直接开放公网。当前许可选择、当前候选真实安装及 Mac 浏览器验收状态应查本轮独立报告，历史记录不代替本轮验收。
