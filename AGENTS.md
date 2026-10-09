# 开发说明

本文件给开发者和 AI 编码助手使用，描述在本仓库工作时需要遵守的约定。

## 开始前

先阅读 `README.md` 和 `docs/ARCHITECTURE.zh-CN.md`。维护者的计划与进度笔记放在本机的 `docs/dev/`（被 `.gitignore` 忽略，不纳入版本库）；存在时一并阅读，以其中的范围和决策为准。

## 数据范围

开发时可以用小样数据验证，但这只适用于开发验证。正常使用中 SEC 申报原文和交易数据完整保留，并按设定的范围采集和回补；行情日线是 24 小时缓存（按需一次取完整区间，24 小时后连同由它算出的分析结果一起删除），不做永久保存或版本化。默认值见 `config/collection-defaults.toml`。不要把测试限制写成产品限制，也不要把数据样本称为完整覆盖。

## 运行与测试

- 使用 `./iirp start|stop|status|check`（Linux 与 macOS 都走 Docker Compose，默认项目 `iirp2`，实现在 `scripts/cli.py`）；`./iirp python`、`./iirp lint`、`./iirp frontend` 在锁定镜像中运行开发工具。
- 后端代码在 `backend/iirp`。单独运行 Python 命令时设置 `PYTHONPATH=backend`。
- 测试使用隔离的 `iirp_v1_test_*` 数据库，不访问运行库：`./iirp check` 和 `./iirp test [pytest 参数]` 启动临时 PostgreSQL 容器（tmpfs），不在正式实例的数据库服务器上建库。共享路径改动后重新跑集成测试。
- 运行配置和密钥（`deploy/.env` 等）被 `.gitignore` 忽略，不要打印或提交。`./iirp start` 在没有该文件时从 `deploy/.env.example` 生成（权限 600、随机数据库密码）；SEC 联系信息可以写在其中的 `IIRP_SEC_USER_AGENT`（优先），也可以在界面填写（存本机数据库）。
- 数据库迁移从 `migrations/versions/0001_baseline.py` 开始，新改动写成新的迁移；`tests/jobs/test_lifecycle.py` 用 `alembic check` 核对模型与迁移一致。
- 前端类型从 `docs/openapi.json` 生成；不要在 TypeScript 中重复实现金融计算。
- 界面文字只写在 `frontend/src/locales/{en,zh}/translation.json`；`scripts/check_ui_text.py`（在 pytest 中运行）拒绝前端源码里的中文字符和成句英文。
- 后端返回给用户的文字一律用 `iirp.messages` 的消息代码（`msg`、`UserError`、`NotFoundError`），不写成句的中文或英文；新代码的文字加到 `frontend/src/locales/{en,zh}/translation.json`，`tests/test_messages.py` 会检查。

## 工作方式

- 任务控制要持久且带围栏（租约令牌）；并行工作只分配给互相独立的写入范围。
- 不要修改正在运行的实例或其数据库，除非已明确授权；数据库迁移先备份并在恢复副本上演练。
- 未经授权不推送、不公开发布；不创建远程仓库、不删除分支或引用。
- 不要提交本机路径、主机名、个人邮箱或任何秘密；过程证据、验证截图和日志放在仓库外（README 用的截图在 `docs/images/`，只截公开数据或示例数据）。

## 提交与 PR

- 提交说明、PR 标题和描述用英文，格式为 Conventional Commits：`feat`、`fix`、`perf`、`refactor`、`docs`、`test`、`build`、`ci`、`chore`，可带范围（如 `fix(insider): ...`）。首行用祈使句，不超过 72 个字符，写清改了什么；需要时在正文写原因和影响。
- 只描述代码和行为的变化，不写开发过程：不写任务或阶段代号、会话、工具额度，也不写“改名”“清理”之类的内部经过。
- 代码注释、文档和测试要自成一体：直接写明规则，不引用本机笔记里的编号（例如任务或决策编号）。
- 一个 PR 做一件事，用 squash 合并，PR 标题即合并后的提交说明。
