#!/bin/bash
# Claude Code Pool 启动脚本
# 在首次启动时初始化 Claude 配置，跳过 onboarding

# 等待 .env 文件中的配置加载完成
sleep 2

# 检查是否已有初始化标记
if [ ! -f /root/.claude/.initialized ]; then
    echo "[启动脚本] 首次启动，初始化 Claude 配置..."

    # 预执行一次简单的 claude 命令来触发初始化（使用 -p 模式跳过交互）
    # 即使 API 配置未生效，也会创建必要的状态文件
    claude -p "echo initialized" \
        --permission-mode dontAsk \
        --allowed-tools Read,Bash \
        > /dev/null 2>&1 || true

    # 创建初始化标记
    touch /root/.claude/.initialized
    echo "[启动脚本] 初始化完成"
fi

# 启动主程序
exec python /app/server.py
