# Claude Code Pool 镜像
# 预安装 Claude Code CLI 和相关依赖

FROM python:3.11-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 使用清华镜像源加速
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources

# 安装 Node.js 和 npm（使用清华 NodeSource 镜像）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    git \
    && curl -fsSL https://mirrors.tuna.tsinghua.edu.cn/nodesource/deb/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 安装 Claude Code CLI（使用淘宝 npm 镜像）
RUN npm install -g @anthropic-ai/claude-code --registry=https://registry.npmmirror.com \
    && npm cache clean --force

# 验证安装
RUN claude --version

# 创建 Claude Code 配置目录
RUN mkdir -p /root/.claude

# 安装 Python 依赖（使用清华镜像源）
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制应用代码
COPY server.py .
COPY start.sh /start.sh
RUN chmod +x /start.sh

# 创建站点目录
RUN mkdir -p /sites && chmod 755 /sites

# 设置环境变量（可在 docker-compose 中覆盖）
ENV POOL_SIZE=3 \
    CLAUDE_TIMEOUT=300 \
    CLAUDE_API_KEY=""

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/status')" || exit 1

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["/start.sh"]
