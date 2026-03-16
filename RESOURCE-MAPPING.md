# 资源映射配置指南

## Docker Compose 卷映射

在 `docker-compose.yml` 中配置以下卷映射：

```yaml
services:
  claude-code-pool:
    volumes:
      # 输出目录
      - ./output:/sites

      # Claude 配置文件映射
      - ./settings.json:/root/.claude/settings.json:ro

      # Skills 目录映射（可选）
      - ./skills:/root/.claude/skills:ro

      # 全局 CLAUDE.md 映射（可选）
      - ./CLAUDE.md:/root/.claude/CLAUDE.md:ro
```

## 目录结构说明

```
claude-code-pool/
├── settings.json              # Claude 工具配置（不含敏感信息）
├── CLAUDE.md                  # 项目规范（可选）
├── skills/                    # Skills 目录（可选）
│   └── your-skill.md
└── output/                    # 任务输出目录
    └── task-xxx/              # 任务生成的文件
```

## 注意事项

1. **敏感信息**：`ANTHROPIC_AUTH_TOKEN` 等敏感信息通过 `.env` 文件或环境变量注入，不要写入 `settings.json`
2. **只读映射**：配置文件使用 `:ro` 只读模式挂载
3. **Skills 加载**：在请求中指定 `skills` 参数即可使用对应技能
4. **工作目录**：确保目标目录有写入权限
