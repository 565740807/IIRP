# 开发说明

本文件给开发者和 AI 编码助手使用，描述在本仓库工作时需要遵守的约定。

## 开始前

先阅读 `docs/PRODUCT_AND_IMPLEMENTATION_PLAN.zh-CN.md`、`docs/DECISIONS.md`、`docs/ARCHITECTURE.zh-CN.md` 和 `docs/IMPROVEMENT_PLAN.zh-CN.md`（含进度表）。这是一个独立的新项目，不要整体导入旧项目的迁移、运行目录或预发布数据。

## 数据范围

开发时可以用小样数据验证，但这只适用于开发验证。正常使用必须保留完整的采集、回补和按需历史目标，默认值见 `config/collection-defaults.toml`。不要把测试限制写成产品限制，也不要把数据样本称为完整覆盖，或把阶段性成果称为完整 V1。

## 运行与测试

- 使用 `./iirp start|stop|status|check`（Linux 封装使用 Compose 项目 `iirp2`）；`./iirp python`、`./iirp lint`、`./iirp frontend` 在锁定镜像中运行开发工具。
- 后端代码在 `backend/iirp`。单独运行 Python 命令时设置 `PYTHONPATH=backend`。
- 测试使用隔离的 `iirp_v1_test_*` 数据库，不访问运行库。共享路径改动后重新跑集成测试。
- 运行配置和密钥（`deploy/.env` 等）被 `.gitignore` 忽略，不要打印或提交。
- 前端类型从 `docs/openapi.json` 生成；不要在 TypeScript 中重复实现金融计算。

## 工作方式

- 任务控制要持久且带围栏（租约令牌）；并行工作只分配给互相独立的写入范围。
- 不要修改正在运行的实例或其数据库，除非已明确授权；数据库迁移先备份并在恢复副本上演练。
- 未经授权不推送、不公开发布；不创建远程仓库、不删除分支或引用。
- 不要提交本机路径、主机名、个人邮箱或任何秘密；过程证据、截图和日志放在仓库外。
