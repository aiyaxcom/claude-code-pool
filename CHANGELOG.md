# 更新日志 (Changelog)

## [v0.1.0] - 2026-03-19

### 新增功能
- 并发控制：支持限制同时执行的 Claude Code 任务数量
- 自动授权：无需人工交互，自动批准工具调用（`--permission-mode dontAsk`）
- HTTP API：暴露 HTTP API 接收请求，支持同步/异步任务
- Skills 支持：支持加载自定义 Skills 扩展能力
- 任务管理：完整的任务状态跟踪和历史记录
- 数据库持久化：支持 PostgreSQL/SQLite 持久化任务状态
- WebSocket/SSE：支持实时流式输出任务进度
- 启动脚本：`start.sh` 自动初始化 Claude 配置，跳过 onboarding

### API 端点
- `GET /status` - 服务状态
- `GET /tasks` - 任务列表
- `GET /tasks/{task_id}` - 单个任务状态
- `GET /tasks/{task_id}/detail` - 任务详细状态（含执行活动跟踪）
- `GET /tasks/{task_id}/stream` - SSE 流式输出
- `WS /ws/tasks/{task_id}` - WebSocket 流式输出
- `POST /task` - 创建异步任务
- `POST /execute` - 同步执行任务

### 配置优化
- 支持多种模型：`claude-opus-4-6`、`claude-sonnet-4-6`、`claude-haiku-4-5`、`qwen3.5-plus`
- 支持兼容 API：通义千问等 Anthropic API 兼容接口
- 环境变量：`CLAUDE_API_KEY` 与 `ANTHROPIC_AUTH_TOKEN` 等效
- settings.json：`model` 字段改为字符串类型

### 文档更新
- README.md：补充环境变量详解、启动脚本说明、API 端点、FAQ
- .env.example：添加详细中文注释
- settings.json.example：更新为正确的字符串格式
- CLAUDE.md.example：重写为通用项目规范

### 移除功能
- 主动轮询模式：简化代码和维护，由后端服务管理任务提交

### 技术栈
- Python 3.11+
- FastAPI
- Docker / Docker Compose
- PostgreSQL / SQLite
