# Claude Code Pool

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Docker](https://img.shields.io/badge/docker-latest-blue?logo=docker)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![CI/CD](https://img.shields.io/github/actions/workflow/status/aiyaxcom/claude-code-pool/docker-image.yml?branch=main&label=build)

Claude Code Pool 是一个基于 Claude Code CLI 的服务池管理系统，支持并发控制、自动授权和任务队列。

## 功能特性

- **并发控制**：支持限制同时执行的 Claude Code 任务数量
- **自动授权**：无需人工交互，自动批准工具调用
- **双模式运行**：
  - **被动模式**：暴露 HTTP API 接收请求
  - **主动模式**：从配置的外部 API 轮询获取任务
- **Skills 支持**：支持加载自定义 Skills 扩展能力
- **任务管理**：完整的任务状态跟踪和历史记录
- **通用任务执行**：不限特定场景，可执行各种编码任务

## Docker 镜像使用

### 镜像地址

```bash
docker pull aiyax/claude-code-pool:latest
```

**注意**：GitHub Actions 会自动推送镜像到 Docker Hub，需要在 GitHub 仓库的 Settings → Secrets and variables → Actions 中配置以下 secrets：
- `DOCKERHUB_USERNAME`: Docker Hub 用户名（例如：`aiyax`）
- `DOCKERHUB_TOKEN`: Docker Hub Access Token

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
      - POOL_SIZE=3
      - CLAUDE_AUTO_APPROVE=all
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

### 任务列表

```bash
GET /tasks
```

### 单个任务状态

```bash
GET /tasks/{task_id}
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

## 配置说明

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ANTHROPIC_AUTH_TOKEN` | - | **必填** Claude API 密钥 |
| `ANTHROPIC_BASE_URL` | https://api.anthropic.com | Claude API 基础 URL |
| `ANTHROPIC_MODEL` | claude-sonnet-4-5 | 模型名称 |
| `POOL_SIZE` | 3 | 并发任务数限制 |
| `CLAUDE_TIMEOUT` | 300 | 任务超时时间（秒） |
| `CLAUDE_AUTO_APPROVE` | all | 自动授权模式：all/none/selective |
| `TASK_POLL_ENABLED` | false | 是否启用主动轮询模式 |
| `TASK_POLL_URL` | - | 轮询任务的外部 API URL |
| `TASK_POLL_INTERVAL` | 5 | 轮询间隔（秒） |
| `TASK_POLL_API_KEY` | - | 轮询 API 认证密钥 |
| `TASK_POLL_CALLBACK_URL` | - | 任务完成回调 URL |
| `WORKSPACE_ROOT` | /workspace | 工作目录根路径 |
| `OUTPUT_ROOT` | /sites | 输出目录根路径 |

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

## 主动轮询模式

启用主动轮询模式后，服务会定期从配置的外部 API 获取任务：

```bash
# 启用轮询模式
TASK_POLL_ENABLED=true
TASK_POLL_URL=https://your-api.com/api/tasks/pending
TASK_POLL_INTERVAL=5
TASK_POLL_API_KEY=your-api-key
TASK_POLL_CALLBACK_URL=https://your-api.com/api/tasks/callback
```

轮询逻辑：
1. 定期调用 `TASK_POLL_URL` 获取待处理任务
2. 自动执行任务并更新状态
3. 任务完成后回调通知外部 API

## Skills 使用

将 skill 文件放在挂载的 skills 目录下，然后在请求中指定：

```json
{
  "skills": ["artifacts-builder", "brand-guidelines"]
}
```

Skills 文件通过 Docker 卷挂载到 `/root/.claude/skills/` 目录。

## 自动授权说明

### 模式说明

1. **all** - 自动批准所有工具调用和命令
2. **none** - 不自动批准任何操作（需要人工交互）
3. **selective** - 仅批准指定的工具和命令

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

### 常见问题

**Q: 任务一直处于 pending 状态**
A: 检查并发限制（POOL_SIZE），可能有其他任务正在执行。

**Q: 自动授权不生效**
A: 检查 `CLAUDE_AUTO_APPROVE` 环境变量设置，确认 settings.json 中的 autoApprove 配置。

**Q: 找不到 claude 命令**
A: 确认 Dockerfile 中已正确安装 Claude Code CLI，检查容器内 `claude --version` 输出。

**Q: 任务执行失败**
A: 查看任务详细日志，确认目标目录有写入权限，Claude API 密钥有效。

## License

MIT
