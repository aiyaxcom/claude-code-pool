# Claude Code Pool

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Docker](https://img.shields.io/badge/docker-latest-blue?logo=docker)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![CI/CD](https://img.shields.io/github/actions/workflow/status/aiyaxcom/claude-code-pool/docker-image.yml?branch=main&label=build)

Claude Code Pool 是一个基于 Claude Code CLI 的服务池管理系统，支持并发控制、自动授权和任务队列。

## 功能特性

- **并发控制**：支持限制同时执行的 Claude Code 任务数量
- **自动授权**：无需人工交互，自动批准工具调用
- **HTTP API**：暴露 HTTP API 接收请求
- **Skills 支持**：支持加载自定义 Skills 扩展能力
- **任务管理**：完整的任务状态跟踪和历史记录
- **数据库持久化**：支持 PostgreSQL/SQLite 持久化任务状态，服务重启后可恢复
- **通用任务执行**：不限特定场景，可执行各种编码任务

## Docker 镜像使用

### 镜像地址

```bash
docker pull aiyax/claude-code-pool:latest
```

### 快速开始

```bash
docker run -d \
  --name claude-code-pool \
  -p 8000:8000 \
  -e ANTHROPIC_AUTH_TOKEN=sk-ant-xxxxx \
  -e POOL_SIZE=3 \
  -e CLAUDE_AUTO_APPROVE=all \
  -v ./settings.json:/root/.claude/settings.json:ro \
  -v ./skills:/root/.claude/skills:ro \
  -v ./CLAUDE.md:/root/.claude/CLAUDE.md:ro \
  aiyax/claude-code-pool:latest
```

### Docker Compose 示例

```yaml
services:
  claude-code-pool:
    image: aiyax/claude-code-pool:latest
    container_name: claude-code-pool
    restart: always
    ports:
      - "8000:8000"
    environment:
      - ANTHROPIC_AUTH_TOKEN=sk-ant-xxxxx
      - CLAUDE_API_KEY=sk-ant-xxxxx  # 或使用 CLAUDE_API_KEY
      - POOL_SIZE=3
      - CLAUDE_TIMEOUT=600          # 任务超时时间（秒）
      - CLAUDE_AUTO_APPROVE=all     # 自动授权模式
      - WORKSPACE_ROOT=/workspace   # 工作目录
      - OUTPUT_ROOT=/sites          # 输出目录
      - TIMEZONE_OFFSET=8           # 时区（东八区）
    volumes:
      - ./settings.json:/root/.claude/settings.json:ro
      - ./skills:/root/.claude/skills:ro
      - ./CLAUDE.md:/root/.claude/CLAUDE.md:ro
```

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ANTHROPIC_AUTH_TOKEN` | - | **必填** Claude API 密钥 |
| `ANTHROPIC_BASE_URL` | https://api.anthropic.com | Claude API 基础 URL |
| `ANTHROPIC_MODEL` | claude-sonnet-4-5 | 模型名称 |
| `POOL_SIZE` | 3 | 并发任务数限制 |
| `CLAUDE_TIMEOUT` | 300 | 任务超时时间（秒） |
| `CLAUDE_AUTO_APPROVE` | all | 自动授权模式：all/none/selective |

## 快速开始

### 1. 配置环境变量

复制 `.env.example` 为 `.env` 并填写真实值：

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
# Claude API 认证（必填）
ANTHROPIC_AUTH_TOKEN=sk-ant-xxxxx

# Claude API 基础 URL（可选）
ANTHROPIC_BASE_URL=https://api.anthropic.com

# 模型名称（可选）
ANTHROPIC_MODEL=claude-sonnet-4-5
```

### 使用国产大模型（如通义千问）

本项目支持使用兼容 Anthropic API 格式的国产大模型：

```bash
# 通义千问（阿里云百炼）
# 官网：https://www.aliyun.com/product/bailian
ANTHROPIC_AUTH_TOKEN=sk-xxxxx
ANTHROPIC_BASE_URL=https://coding.dashscope.aliyuncs.com/apps/anthropic
ANTHROPIC_MODEL=qwen3.5-plus
```

**注意**：需要确保模型支持 Anthropic API 兼容模式

### 2. 使用 Docker Compose 启动

```bash
docker compose up -d
```

### 3. 查看服务状态

```bash
curl http://localhost:8000/status
```

## API 端点

### 服务状态

```bash
GET /status
```

响应：
```json
{
  "pool_size": 3,
  "active_tasks": 0,
  "available_slots": 3,
  "poll_enabled": false
}
```

> 注意：`poll_enabled` 字段固定为 `false`，因为主动轮询功能已移除。

### 任务列表

```bash
GET /tasks
```

### 单个任务状态

```bash
GET /tasks/{task_id}
```

### 任务详细状态（包含执行活动跟踪）

```bash
GET /tasks/{task_id}/detail
```

响应：
```json
{
  "task_id": "abc12345",
  "status": "running",
  "prompt": "创建一个 Python 脚本",
  "target_dir": "/workspace/myproject",
  "created_at": "2024-01-01T12:00:00+08:00",
  "updated_at": "2024-01-01T12:01:00+08:00",
  "activity": {
    "last_activity": "2024-01-01T12:01:00+08:00",
    "stage": "executing"
  },
  "output": "任务执行输出..."
}
```

### 创建异步任务

```bash
POST /task
Content-Type: application/json

{
  "prompt": "创建一个 Python 脚本，实现简单的 HTTP 服务器",
  "target_dir": "/workspace/myproject",
  "system_prompt": "你是一个 Python 专家",
  "auto_approve": true,
  "skills": ["python-expert"]
}
```

响应：
```json
{
  "task_id": "abc12345",
  "status": "pending",
  "message": "任务已创建，正在排队执行"
}
```

### 同步执行任务

```bash
POST /execute
Content-Type: application/json

{
  "prompt": "修复 bug：xxx",
  "target_dir": "/workspace/myproject"
}
```

### WebSocket 流式输出

```bash
WS /ws/tasks/{task_id}
```

连接后实时接收任务执行输出。

### SSE 流式输出

```bash
GET /tasks/{task_id}/stream
```

通过 Server-Sent Events 接收任务执行进度。

## 配置说明

### 环境变量详解

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ANTHROPIC_AUTH_TOKEN` | - | **必填** Claude API 密钥（或 `CLAUDE_API_KEY`） |
| `CLAUDE_API_KEY` | - | **必填** Claude API 密钥（与 `ANTHROPIC_AUTH_TOKEN` 等效） |
| `ANTHROPIC_BASE_URL` | https://api.anthropic.com | Claude API 基础 URL，可使用兼容接口（如通义千问） |
| `ANTHROPIC_MODEL` | claude-sonnet-4-5 | 模型名称，如 `claude-opus-4-6`、`claude-sonnet-4-6`、`claude-haiku-4-5` |
| `POOL_SIZE` | 3 | 并发任务数限制 |
| `CLAUDE_TIMEOUT` | 300 | 任务超时时间（秒），超时后强制终止 |
| `CLAUDE_AUTO_APPROVE` | all | 自动授权模式：all（全部自动批准）/ none（需要人工确认）/ selective（选择性批准） |
| `WORKSPACE_ROOT` | /workspace | 工作目录根路径，Claude Code 在此目录下操作 |
| `OUTPUT_ROOT` | /sites | 输出目录根路径，任务结果保存位置 |
| `DATABASE_URL` | - | 数据库连接 URL（可选，用于持久化任务状态），支持 PostgreSQL/SQLite |
| `TIMEZONE_OFFSET` | 8 | 时区偏移量（东八区为 8），用于数据库时间戳 |

### 启动脚本说明

`start.sh` 在容器启动时自动执行：

```bash
#!/bin/bash
# 1. 检查是否已初始化（/root/.claude/.initialized 标记）
# 2. 首次启动时执行 claude -p "echo initialized" 触发初始化
# 3. 使用 --permission-mode dontAsk 跳过权限确认
# 4. 创建初始化标记文件
# 5. 启动主程序 server.py
```

初始化目的：
- 跳过 Claude Code CLI 的 onboarding 流程
- 创建必要的配置目录和状态文件
- 确保后续任务执行不会被交互提示阻塞

### 目录结构

```
claude-code-pool/
├── server.py              # 主服务程序
├── Dockerfile             # Docker 镜像
├── requirements.txt       # Python 依赖
├── .env.example           # 环境变量示例
├── settings.json.example  # Claude 配置示例
├── CLAUDE.md.example      # 项目规范示例
├── RESOURCE-MAPPING.md    # 资源映射指南
├── .github/workflows/     # GitHub Actions
└── README.md              # 本文档
```

### settings.json 配置

```json
{
  "allowedTools": [
    "Read", "Write", "Edit", "Bash", "Glob", "Grep", "Task", "WebFetch", "WebSearch"
  ],
  "autoApprove": {
    "enabled": true,
    "tools": ["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
    "commands": ["ls", "cat", "head", "tail", "grep", "find", "mkdir", "cp", "mv", "git status", "git diff"]
  },
  "model": {
    "maxTokens": 8192,
    "temperature": 0.7
  },
  "permissions": {
    "write": true,
    "execute": true,
    "network": true
  }
}
```

**注意**：`--permission-mode dontAsk` 参数需要在启动时通过 `start.sh` 脚本传入，确保跳过权限确认。

### 调试日志

服务启动时会输出详细配置信息，用于调试：

```
[CONFIG] CLAUDE_TIMEOUT=600 秒
[CONFIG] OUTPUT_ROOT=/sites
[CONFIG] WORKSPACE_ROOT=/workspace
[CONFIG] ANTHROPIC_BASE_URL=未设置
[CONFIG] ANTHROPIC_MODEL=未设置
[CONFIG] CLAUDE_API_KEY=sk-ant-... (前 15 位)
```

如果看到 API_KEY 前缀输出，说明配置已正确加载。

## 数据库持久化

服务支持使用 PostgreSQL 或 SQLite 持久化任务状态，任务状态在服务重启后可恢复。

### 配置数据库

通过 `DATABASE_URL` 环境变量配置数据库连接：

```bash
# PostgreSQL 示例
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/claude_code_pool

# SQLite 示例
DATABASE_URL=sqlite+aiosqlite:///tasks.db
```

### 持久化行为

- **自动建表**：服务启动时自动创建任务表（如果不存在）
- **任务恢复**：重启时自动恢复 `pending` 和 `running` 状态的任务到内存
- **失败任务处理**：`failed` 状态的任务**不会**自动恢复，需要手动处理
- **状态同步**：任务状态变更会同步写入数据库
- **时间戳处理**：数据库时间戳字段自动去除时区信息（与本地时区对齐）

### Docker Compose 配置示例

```yaml
services:
  claude-code-pool:
    image: aiyax/claude-code-pool:latest
    container_name: claude-code-pool
    restart: always
    ports:
      - "8000:8000"
    environment:
      - ANTHROPIC_AUTH_TOKEN=sk-ant-xxxxx
      - POOL_SIZE=3
      - CLAUDE_AUTO_APPROVE=all
      - DATABASE_URL=postgresql+asyncpg://user:password@postgres:5432/claude_code_pool
      - TIMEZONE_OFFSET=8
    volumes:
      - ./settings.json:/root/.claude/settings.json:ro
      - ./skills:/root/.claude/skills:ro
      - ./CLAUDE.md:/root/.claude/CLAUDE.md:ro
```

## 主动轮询模式（已移除）

> 注意：主动轮询模式已在最新版本中移除，因为大多数场景下后端服务会直接管理任务提交和轮询。

旧版本的主动轮询功能允许服务从外部 API 获取任务，该功能已被移除以简化代码和维护。

## Skills 使用

将 skill 文件放在挂载的 skills 目录下，然后在请求中指定：

```json
{
  "skills": ["artifacts-builder", "brand-guidelines"]
}
```

Skills 文件通过 Docker 卷挂载到 `/root/.claude/skills/` 目录。

### Skill 目录结构

```
skills/
└── your-skill/
    ├── SKILL.md           # Skill 定义（必需）
    ├── scripts/           # 脚本文件（可选）
    ├── references/        # 参考文档（可选）
    └── assets/            # 资源文件（可选）
```

**注意**：新版 Claude Code CLI 移除了 `--skill` 参数支持，Skills 通过预挂载方式加载。

## 自动授权说明

### 模式说明

1. **all** - 自动批准所有工具调用和命令（推荐用于可信环境）
2. **none** - 不自动批准任何操作（需要人工交互）
3. **selective** - 仅批准指定的工具和命令

### CLI 参数说明

启动时通过以下参数控制权限：

```bash
claude -p "your prompt" \
    --permission-mode dontAsk \
    --allowed-tools Read,Bash,Write \
    --verbose
```

- `--permission-mode dontAsk`: 跳过权限确认提示
- `--allowed-tools`: 指定允许使用的工具
- `--verbose`: 输出详细日志（用于调试）

### 安全建议

- 生产环境建议使用 `selective` 模式
- 明确指定允许的工具和命令列表
- 定期审查任务执行日志

## 使用示例

### Docker Compose 配置

```yaml
services:
  claude-code-pool:
    image: your-username/claude-code-pool:latest
    container_name: claude-code-pool
    restart: always
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      # 输出目录
      - ./output:/sites
      # Claude 配置映射
      - ./settings.json:/root/.claude/settings.json:ro
      - ./skills:/root/.claude/skills:ro
      # 可选：全局 CLAUDE.md 映射
      - ./CLAUDE.md:/root/.claude/CLAUDE.md:ro
```

### Python 客户端示例

```python
import httpx

# 创建异步任务
response = httpx.post("http://localhost:8000/task", json={
    "prompt": "创建一个 Flask 应用",
    "target_dir": "/workspace/flask-app"
})
task_id = response.json()["task_id"]

# 查询任务状态
status = httpx.get(f"http://localhost:8000/tasks/{task_id}")
print(status.json())
```

### cURL 示例

```bash
# 同步执行
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "创建 README.md 文件",
    "target_dir": "/workspace/myproject"
  }'

# 创建异步任务
curl -X POST http://localhost:8000/task \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "重构代码，提取公共函数",
    "target_dir": "/workspace/myproject"
  }'
```

## 故障排查

### 查看日志

```bash
docker compose logs -f claude-code-pool
```

### 检查服务状态

```bash
curl http://localhost:8000/status
```

### 进入容器调试

```bash
docker compose exec claude-code-pool bash
```

### 调试模式

启动时添加 `--verbose` 参数输出详细日志：

```bash
# 在 server.py 中或通过环境变量启用详细日志
# 查看 Claude Code CLI 的详细输出
```

### 常见问题

**Q: 任务一直处于 pending 状态**
A: 检查并发限制（POOL_SIZE），可能有其他任务正在执行。

**Q: 自动授权不生效**
A: 检查 `CLAUDE_AUTO_APPROVE` 环境变量设置，确认 settings.json 中的 autoApprove 配置。确保启动脚本使用 `--permission-mode dontAsk` 参数。

**Q: 找不到 claude 命令**
A: 确认 Dockerfile 中已正确安装 Claude Code CLI，检查容器内 `claude --version` 输出。

**Q: 任务执行失败，报错 "Waiting for approval"**
A: 确保启动脚本使用 `--permission-mode dontAsk` 参数，或检查 `CLAUDE_AUTO_APPROVE=all` 设置。

**Q: 数据库连接失败**
A: 检查 `DATABASE_URL` 格式是否正确，确保数据库服务可访问。PostgreSQL 需要使用 `postgresql+asyncpg://` 前缀。

**Q: 输出中文乱码**
A: 检查 `TIMEZONE_OFFSET` 环境变量设置，确保与数据库时区一致。

**Q: WebSocket 连接断开**
A: 检查任务是否已完成执行，WebSocket 仅在任务执行期间保持连接。

## License

MIT
