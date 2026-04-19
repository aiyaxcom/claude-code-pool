"""
Claude Code Pool - 服务池管理服务器

支持两种运行模式：
1. 被动模式：暴露 HTTP API 接收请求
2. 主动模式：从配置的外部 API 轮询获取任务

功能特性：
- 并发控制（信号量限制）
- 自动授权（无需人工交互）
- 任务队列和状态跟踪
- Skills 支持
- 通用任务执行（不限 AI 建站）
"""
import asyncio
import os
import json
import uuid
import time
import httpx
import logging
from pathlib import Path
from typing import Optional, Dict, List, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# 数据库支持（可选）
try:
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import Column, Integer, String, DateTime, Text, Index
    from sqlalchemy.orm import declarative_base
    from datetime import datetime, timezone, timedelta
    DATABASE_AVAILABLE = True
except ImportError:
    DATABASE_AVAILABLE = False
    create_async_engine = None
    async_sessionmaker = None
    AsyncSession = None
    declarative_base = None
    datetime = None


# ==================== 配置 ====================

# 并发控制
POOL_SIZE = int(os.getenv("POOL_SIZE", "3"))
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))  # 空闲超时（无输出时）
MAX_TOTAL_TIMEOUT = int(os.getenv("MAX_TOTAL_TIMEOUT", "3600"))  # 最大总执行时间（默认1小时）

# 调试模式
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# Claude Code 配置
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_AUTO_APPROVE = os.getenv("CLAUDE_AUTO_APPROVE", "all")  # all, none, selective
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", "/workspace")
OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "/sites")

# 打印配置信息（用于调试）
print(f"[CONFIG] CLAUDE_TIMEOUT={CLAUDE_TIMEOUT} 秒（空闲超时）")
print(f"[CONFIG] MAX_TOTAL_TIMEOUT={MAX_TOTAL_TIMEOUT} 秒（最大执行时间）")
print(f"[CONFIG] OUTPUT_ROOT={OUTPUT_ROOT}")
print(f"[CONFIG] WORKSPACE_ROOT={WORKSPACE_ROOT}")
print(f"[CONFIG] ANTHROPIC_BASE_URL={os.getenv('ANTHROPIC_BASE_URL', '未设置')}")
print(f"[CONFIG] ANTHROPIC_MODEL={os.getenv('ANTHROPIC_MODEL', '未设置')}")

# 打印 API_KEY 前缀（用于验证）
_api_key = os.getenv('CLAUDE_API_KEY') or os.getenv('ANTHROPIC_AUTH_TOKEN') or ''
if _api_key:
    _prefix = _api_key[:15] + '...' if len(_api_key) > 15 else _api_key
    print(f"[CONFIG] CLAUDE_API_KEY={_prefix} (前 15 位)")
else:
    print(f"[CONFIG] CLAUDE_API_KEY=未设置")

# 数据库配置（可选，用于持久化任务状态）
DATABASE_URL = os.getenv("DATABASE_URL", "")  # PostgreSQL: postgresql+asyncpg://... 或 SQLite: sqlite+aiosqlite:///tasks.db

# 信号量控制并发
semaphore = asyncio.Semaphore(POOL_SIZE)

# 当前活跃的任务计数
active_tasks = 0

# 任务执行记录（用于跟踪状态，内存缓存）
task_registry: Dict[str, dict] = {}

# WebSocket 连接管理（用于流式输出任务进度）
task_websockets: Dict[str, List[WebSocket]] = {}

# 任务输出日志存储（用于 SSE/轮询）
task_outputs: Dict[str, List[dict]] = {}

# 数据库引擎和会话工厂（可选）
db_engine = None
AsyncSessionLocal = None

# 时区配置（默认东八区）
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "8"))  # 时区偏移量，东八区为 8
TZ_LOCAL = timezone(timedelta(hours=TIMEZONE_OFFSET))


def get_local_datetime() -> datetime:
    """获取本地时区当前时间（无时区信息，适配 PostgreSQL TIMESTAMP WITHOUT TIME ZONE）"""
    return datetime.now(TZ_LOCAL).replace(tzinfo=None)


# ==================== 数据模型 ====================

# SQLAlchemy Base
if DATABASE_AVAILABLE:
    Base = declarative_base()

    class TaskModel(Base):
        """任务数据库模型"""
        __tablename__ = "tasks"

        id = Column(Integer, primary_key=True, autoincrement=True)
        task_id = Column(String(64), unique=True, nullable=False, index=True)
        task_type = Column(String(32), default="custom")
        status = Column(String(32), default="pending")  # pending, running, completed, failed
        prompt = Column(Text, nullable=False)
        target_dir = Column(String(512), nullable=True)
        system_prompt = Column(Text, nullable=True)
        result = Column(Text, nullable=True)  # JSON string
        summary = Column(Text, nullable=True)  # 任务总结（markdown 格式，从 CLI result 字段提取）
        error = Column(Text, nullable=True)
        created_at = Column(DateTime, default=get_local_datetime)
        started_at = Column(DateTime, nullable=True)
        completed_at = Column(DateTime, nullable=True)

        __table_args__ = (
            Index("idx_tasks_status", "status"),
            Index("idx_tasks_task_id", "task_id"),
        )


@dataclass
class TaskRecord:
    """任务执行记录（内存）"""
    id: str
    task_type: str = "custom"
    status: str = "pending"  # pending, running, completed, failed
    created_at: datetime = field(default_factory=get_local_datetime)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[dict] = None
    summary: Optional[str] = None  # 任务总结（markdown 格式）
    error: Optional[str] = None


class StatusResponse(BaseModel):
    """服务状态响应"""
    pool_size: int
    active_tasks: int
    available_slots: int


class TaskStatusResponse(BaseModel):
    """任务状态响应"""
    id: str
    task_type: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class CustomTaskRequest(BaseModel):
    """自定义任务请求"""
    task_id: Optional[str] = None
    prompt: str
    target_dir: Optional[str] = None
    system_prompt: Optional[str] = None
    auto_approve: bool = True
    skills: Optional[List[str]] = None
    metadata: Optional[dict] = None


class TaskResponse(BaseModel):
    """任务提交响应"""
    task_id: str
    status: str
    message: str


class ExecuteResponse(BaseModel):
    """同步执行响应"""
    success: bool
    message: str
    output: Optional[str] = None
    error: Optional[str] = None


# ==================== 应用生命周期 ====================

async def migrate_database_schema():
    """迁移数据库 schema - 修复时间戳字段类型，添加新字段"""
    if not DATABASE_AVAILABLE or not DATABASE_URL:
        return

    try:
        db_engine = create_async_engine(DATABASE_URL, echo=False)

        async with db_engine.begin() as conn:
            from sqlalchemy import text

            # 判断数据库类型
            is_postgres = DATABASE_URL.startswith('postgresql')
            is_sqlite = DATABASE_URL.startswith('sqlite')

            if is_postgres:
                # PostgreSQL 迁移逻辑
                result = await conn.execute(text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = 'tasks'
                    )
                """))
                table_exists = result.scalar()

                if not table_exists:
                    print("[MIGRATE] tasks 表不存在，将创建新表")
                    await db_engine.dispose()
                    return

                # 检查列类型
                result = await conn.execute(text("""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = 'tasks'
                    AND column_name IN ('started_at', 'completed_at', 'created_at')
                    ORDER BY column_name
                """))
                columns = result.fetchall()

                # 检查是否是 float8/double precision 类型
                needs_migration = False
                for col_name, data_type in columns:
                    if data_type in ('double precision', 'float8'):
                        needs_migration = True
                        print(f"[MIGRATE] 检测到字段 {col_name} 类型为 {data_type}，需要迁移")

                if needs_migration:
                    print("[MIGRATE] 开始迁移时间戳字段类型...")

                    await conn.execute(text("""
                        ALTER TABLE tasks
                        ALTER COLUMN started_at TYPE TIMESTAMP
                        USING CASE
                            WHEN started_at IS NOT NULL THEN TO_TIMESTAMP(started_at)
                            ELSE NULL
                        END
                    """))

                    await conn.execute(text("""
                        ALTER TABLE tasks
                        ALTER COLUMN completed_at TYPE TIMESTAMP
                        USING CASE
                            WHEN completed_at IS NOT NULL THEN TO_TIMESTAMP(completed_at)
                            ELSE NULL
                        END
                    """))

                    await conn.execute(text("""
                        ALTER TABLE tasks
                        ALTER COLUMN created_at TYPE TIMESTAMP
                        USING TO_TIMESTAMP(created_at)
                    """))

                    await conn.execute(text("""
                        ALTER TABLE tasks
                        ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP
                    """))

                    print("[MIGRATE] 时间戳字段迁移完成")

                # 检查并添加 summary 列
                result = await conn.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'tasks'
                    AND column_name = 'summary'
                """))
                summary_exists = result.fetchone()

                if not summary_exists:
                    print("[MIGRATE] 添加 summary 列...")
                    await conn.execute(text("""
                        ALTER TABLE tasks
                        ADD COLUMN summary TEXT
                    """))
                    print("[MIGRATE] summary 列添加完成")

            elif is_sqlite:
                # SQLite 迁移逻辑
                result = await conn.execute(text("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='tasks'
                """))
                table_exists = result.fetchone()

                if not table_exists:
                    print("[MIGRATE] tasks 表不存在，将创建新表")
                    await db_engine.dispose()
                    return

                # 检查列是否存在
                result = await conn.execute(text("PRAGMA table_info(tasks)"))
                columns = [row[1] for row in result.fetchall()]

                if 'summary' not in columns:
                    print("[MIGRATE] 添加 summary 列...")
                    await conn.execute(text("""
                        ALTER TABLE tasks
                        ADD COLUMN summary TEXT
                    """))
                    print("[MIGRATE] summary 列添加完成")

            print("[MIGRATE] 数据库 schema 迁移完成")

        await db_engine.dispose()

    except Exception as e:
        print(f"[MIGRATE] 迁移失败：{e}，将继续尝试初始化数据库")


async def init_database():
    """初始化数据库"""
    global db_engine, AsyncSessionLocal

    if not DATABASE_AVAILABLE or not DATABASE_URL:
        print("数据库未启用，任务将仅保存在内存中（重启后丢失）")
        return

    try:
        # 先执行数据库迁移（修复时间戳字段类型）
        await migrate_database_schema()

        # 创建数据库引擎（添加连接池配置）
        db_engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_size=10,  # 连接池大小
            max_overflow=20,  # 最大溢出连接数
            pool_pre_ping=True,  # 连接前 ping 测试，自动回收无效连接
            pool_recycle=3600,  # 1 小时后回收连接
        )

        # 创建表
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 创建会话工厂
        AsyncSessionLocal = async_sessionmaker(
            bind=db_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        print(f"数据库已初始化：{DATABASE_URL}")

        # 从数据库恢复未完成的任务
        await recover_tasks_from_db()

    except Exception as e:
        print(f"数据库初始化失败：{e}，任务将仅保存在内存中")
        db_engine = None
        AsyncSessionLocal = None


async def recover_tasks_from_db():
    """从数据库恢复未完成的任务状态到内存"""
    if not AsyncSessionLocal:
        return

    try:
        async with AsyncSessionLocal() as session:
            # 查询所有未完成的任务
            from sqlalchemy import select
            result = await session.execute(
                select(TaskModel).where(
                    TaskModel.status.in_(["pending", "running"])
                )
            )
            tasks = result.scalars().all()

            recovered_count = 0
            for task in tasks:
                # 恢复到内存 registry
                record = TaskRecord(
                    id=task.task_id,
                    task_type=task.task_type,
                    status="pending",  # 重置为 pending，准备重新执行
                    created_at=task.created_at,
                    started_at=None,  # 清空开始时间
                    completed_at=None,  # 清空完成时间
                    error=task.error,
                )
                task_registry[task.task_id] = record

                # 重新执行任务
                print(f"[RECOVER] 恢复任务：task_id={task.task_id}")
                print(f"[RECOVER] prompt={task.prompt[:100]}...")
                print(f"[RECOVER] target_dir={task.target_dir}")

                # 等待一小段时间，确保事件循环已准备好
                await asyncio.sleep(0.1)

                # 使用 ensure_future 确保任务被调度
                asyncio.ensure_future(execute_custom_task(
                    task_id=task.task_id,
                    prompt=task.prompt,
                    target_dir=task.target_dir,
                    system_prompt=task.system_prompt or "",
                    auto_approve=True,
                    skills=None,
                ))
                recovered_count += 1
                print(f"[RECOVER] 任务已调度：task_id={task.task_id}")

            print(f"从数据库恢复了 {recovered_count} 个未完成的任务")

    except Exception as e:
        print(f"恢复任务失败：{e}")
        import traceback
        traceback.print_exc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    print(f"Claude Code Pool 启动，并发限制：{POOL_SIZE}")
    print(f"自动授权：{CLAUDE_AUTO_APPROVE}")

    # 初始化数据库
    await init_database()

    yield

    print("Claude Code Pool 关闭")


app = FastAPI(
    title="Claude Code Pool",
    description="Claude Code CLI 服务池管理系统",
    version="1.0.0",
    lifespan=lifespan,
)


# ==================== 访问日志中间件 ====================

# 不记录访问日志的路径（轮询请求等）
NO_LOG_PATHS = ["/status"]


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """访问日志中间件，过滤轮询请求"""
    start_time = time.time()
    response = await call_next(request)
    process_time = (time.time() - start_time) * 1000

    # 检查是否需要记录日志
    path = request.url.path
    should_log = True
    for no_log_path in NO_LOG_PATHS:
        if path == no_log_path or path.startswith(no_log_path + "/"):
            should_log = False
            break

    # 只记录重要请求
    if should_log:
        if response.status_code >= 400:
            print(f"[{datetime.now().isoformat()}] WARN: {request.method} {path} - {response.status_code} ({process_time:.1f}ms)")
        elif request.method in ["POST", "PUT", "DELETE", "PATCH"]:
            print(f"[{datetime.now().isoformat()}] INFO: {request.method} {path} - {response.status_code} ({process_time:.1f}ms)")

    return response


# ==================== HTTP API 端点 ====================

@app.get("/status", response_model=StatusResponse)
async def get_status():
    """获取服务池状态"""
    return StatusResponse(
        pool_size=POOL_SIZE,
        active_tasks=active_tasks,
        available_slots=POOL_SIZE - active_tasks,
    )


@app.get("/tasks", response_model=List[TaskStatusResponse])
async def list_tasks():
    """获取所有任务列表"""
    return [
        TaskStatusResponse(
            id=record.id,
            task_type=record.task_type,
            status=record.status,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            error=record.error,
        )
        for record in task_registry.values()
    ]


@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str):
    """获取单个任务状态"""
    # 先检查内存 registry
    if task_id in task_registry:
        record = task_registry[task_id]
        return TaskStatusResponse(
            id=record.id,
            task_type=record.task_type,
            status=record.status,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            error=record.error,
        )

    # 如果内存中没有，尝试从数据库获取
    if AsyncSessionLocal:
        try:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import select
                result = await session.execute(
                    select(TaskModel).where(TaskModel.task_id == task_id)
                )
                task = result.scalar_one_or_none()

                if task:
                    return TaskStatusResponse(
                        id=task.task_id,
                        task_type=task.task_type,
                        status=task.status,
                        created_at=task.created_at,
                        started_at=task.started_at,
                        completed_at=task.completed_at,
                        error=task.error,
                    )
        except Exception as e:
            print(f"获取任务失败：{e}")

    raise HTTPException(status_code=404, detail="任务不存在")


@app.get("/tasks/{task_id}/detail")
async def get_task_detail(task_id: str):
    """获取单个任务详细信息（包括当前执行状态）"""
    if task_id not in task_registry:
        raise HTTPException(status_code=404, detail="任务不存在")

    record = task_registry[task_id]
    outputs = task_outputs.get(task_id, [])

    # 获取最近的输出，分析当前正在做什么
    current_activity = None
    recent_activities = []

    for output in reversed(outputs[-10:]):  # 查看最近 10 条输出
        if output.get("subagent_status"):
            recent_activities.append({
                "status": output["subagent_status"],
                "timestamp": output.get("timestamp"),
            })
            if not current_activity:
                current_activity = output["subagent_status"]

    # 根据任务状态和输出分析当前活动
    activity_summary = current_activity or "等待执行"
    if record.status == "running" and outputs:
        # 分析最近的工具调用
        last_output = outputs[-1] if outputs else None
        if last_output:
            activity_summary = f"正在执行 - {last_output.get('subagent_status', '处理中')}"

    return {
        "id": record.id,
        "task_type": record.task_type,
        "status": record.status,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "error": record.error,
        "summary": record.summary,  # 任务总结（markdown 格式）
        "current_activity": activity_summary,
        "recent_activities": recent_activities[:5],  # 最近 5 个活动
        "output_count": len(outputs),
        "metadata": record.result.get("metadata", {}) if record.result else None,
    }


@app.websocket("/ws/tasks/{task_id}")
async def websocket_task_output(websocket: WebSocket, task_id: str):
    """
    WebSocket 连接查看任务输出进度
    连接到特定任务的实时输出流
    """
    await websocket.accept()

    # 将连接添加到任务连接列表
    if task_id not in task_websockets:
        task_websockets[task_id] = []
    task_websockets[task_id].append(websocket)

    # 发送历史输出（如果有）
    if task_id in task_outputs:
        for output in task_outputs[task_id]:
            await websocket.send_json(output)

    try:
        # 保持连接，接收客户端消息
        while True:
            data = await websocket.receive_text()
            # 可以处理客户端发来的命令，比如停止任务
            if data == "stop":
                await websocket.send_json({"type": "info", "data": "收到停止请求"})
    except WebSocketDisconnect:
        pass
    finally:
        # 连接关闭时从列表中移除
        if task_id in task_websockets:
            task_websockets[task_id] = [ws for ws in task_websockets[task_id] if ws != websocket]
            if not task_websockets[task_id]:
                del task_websockets[task_id]


@app.get("/tasks/{task_id}/output")
async def get_task_output(task_id: str, last_id: int = 0):
    """获取任务输出日志（支持轮询）"""
    if task_id not in task_registry:
        raise HTTPException(status_code=404, detail="任务不存在")

    record = task_registry[task_id]
    outputs = task_outputs.get(task_id, [])

    # 返回所有输出或只返回新增的输出
    if last_id > 0:
        outputs = outputs[last_id:]

    # 添加调试日志（仅在 DEBUG_MODE 时输出）
    if DEBUG_MODE:
        print(f"[DEBUG] 获取任务输出：task_id={task_id}, status={record.status}, total_outputs={len(task_outputs.get(task_id, []))}, last_id={last_id}, returning={len(outputs)}")

    return {
        "task_id": task_id,
        "outputs": outputs,
        "total": len(task_outputs.get(task_id, [])),
        "task_status": record.status,  # 添加任务状态
    }


@app.get("/tasks/{task_id}/stream")
async def stream_task_output(task_id: str, request: Request):
    """SSE 流式输出任务日志"""
    if task_id not in task_registry:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def generate():
        last_id = 0
        while True:
            # 检查客户端是否断开连接
            if await request.is_disconnected():
                break

            outputs = task_outputs.get(task_id, [])
            if last_id < len(outputs):
                new_outputs = outputs[last_id:]
                for output in new_outputs:
                    yield f"data: {json.dumps(output)}\n\n"
                last_id = len(outputs)

            # 如果任务已完成，发送结束标志并断开
            record = task_registry.get(task_id)
            if record and record.status in ["completed", "failed"]:
                yield f"data: {json.dumps({'type': 'done', 'status': record.status})}\n\n"
                break

            # 等待一段时间后继续轮询
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@app.post("/task", response_model=TaskResponse)
async def create_task(request: CustomTaskRequest, background_tasks: BackgroundTasks):
    """
    创建自定义任务（异步执行）

    任务会在后台执行，通过 /tasks/{task_id} 查询状态
    """
    task_id = request.task_id or str(uuid.uuid4())[:8]

    print(f"[INFO] 创建任务：task_id={task_id}")

    task_registry[task_id] = TaskRecord(
        id=task_id,
        task_type="custom",
        status="pending",
        result={"metadata": request.metadata},
    )

    # 保存到数据库
    await save_task_to_db(
        task_registry[task_id],
        prompt=request.prompt,
        target_dir=request.target_dir,
        system_prompt=request.system_prompt,
    )

    # 在后台执行任务
    background_tasks.add_task(
        execute_custom_task,
        task_id=task_id,
        prompt=request.prompt,
        target_dir=request.target_dir,
        system_prompt=request.system_prompt,
        auto_approve=request.auto_approve,
        skills=request.skills,
        metadata=request.metadata,
    )

    print(f"[INFO] 任务已创建并加入后台执行：task_id={task_id}")

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="任务已创建，正在排队执行",
    )


@app.post("/execute", response_model=ExecuteResponse)
async def execute_task(request: CustomTaskRequest):
    """
    同步执行任务（等待完成）

    适用于需要立即获取结果的场景
    """
    global active_tasks

    task_id = request.task_id or str(uuid.uuid4())[:8]

    task_registry[task_id] = TaskRecord(
        id=task_id,
        task_type="custom",
        status="pending",
    )

    async with semaphore:
        active_tasks += 1
        task_registry[task_id].status = "running"
        task_registry[task_id].started_at = get_local_datetime()

        try:
            # 确定目标目录
            target_dir = request.target_dir or OUTPUT_ROOT
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)

            # 构建 system prompt
            system_prompt = request.system_prompt or ""

            # 加载 CLAUDE.md（如果存在）
            claude_md_path = "/root/.claude/CLAUDE.md"
            if os.path.exists(claude_md_path):
                with open(claude_md_path, "r") as f:
                    claude_md_content = f.read()
                system_prompt = f"{system_prompt}\n\n## 项目规范\n{claude_md_content}"

            # 执行任务
            stdout, stderr, result_target_dir = await run_claude_code_oneshot(
                prompt=request.prompt,
                target_dir=target_dir,
                system_prompt=system_prompt,
                auto_approve=request.auto_approve,
                skills=request.skills,
                task_id=task_id,
                timeout=CLAUDE_TIMEOUT,
            )

            # 检查是否有错误（stderr 非空说明 CLI 执行失败）
            if stderr and stderr.strip():
                print(f"[ERROR] 任务执行出错：{stderr[:500]}")
                task_registry[task_id].status = "failed"
                task_registry[task_id].error = stderr[:2000]
                task_status = "failed"
            else:
                task_registry[task_id].status = "completed"
                task_status = "completed"

            task_registry[task_id].completed_at = get_local_datetime()
            task_registry[task_id].result = {
                "stdout": stdout,
                "stderr": stderr,
                "target_dir": result_target_dir,
            }

            return ExecuteResponse(
                success=task_status == "completed",
                message="任务执行完成" if task_status == "completed" else f"任务执行失败：{stderr[:200]}",
                output=stdout[:2000] if stdout else None,
                error=stderr[:2000] if stderr else None,
            )

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].completed_at = get_local_datetime()
            task_registry[task_id].error = str(e)
            return ExecuteResponse(
                success=False,
                message=f"任务执行失败：{str(e)}",
                error=str(e),
            )

        finally:
            active_tasks -= 1


# ==================== 调试端点 ====================

@app.get("/debug/env")
async def debug_environment():
    """调试端点：查看当前容器的环境变量（敏感信息已脱敏）"""
    # 获取 API_KEY 并显示前缀
    _api_key = os.getenv('CLAUDE_API_KEY') or os.getenv('ANTHROPIC_AUTH_TOKEN') or ''
    _api_key_display = '未设置'
    if _api_key:
        _api_key_display = _api_key[:15] + '...' if len(_api_key) > 15 else _api_key

    return {
        "ANTHROPIC_BASE_URL": os.getenv("ANTHROPIC_BASE_URL", "未设置"),
        "ANTHROPIC_MODEL": os.getenv("ANTHROPIC_MODEL", "未设置"),
        "CLAUDE_API_KEY": _api_key_display,
        "ANTHROPIC_AUTH_TOKEN": '已设置' if os.getenv('ANTHROPIC_AUTH_TOKEN') else '未设置',
        "POOL_SIZE": os.getenv("POOL_SIZE", "3"),
        "CLAUDE_TIMEOUT": os.getenv("CLAUDE_TIMEOUT", "300"),
        "OUTPUT_ROOT": os.getenv("OUTPUT_ROOT", "/sites"),
        "WORKSPACE_ROOT": os.getenv("WORKSPACE_ROOT", "/workspace"),
    }


# ==================== 任务执行器 ====================

async def execute_custom_task(
    task_id: str,
    prompt: str,
    target_dir: Optional[str],
    system_prompt: Optional[str],
    auto_approve: bool,
    skills: Optional[List[str]],
    metadata: Optional[dict] = None,
):
    """执行自定义任务（后台异步）"""
    global active_tasks

    # 打印任务信息（用于调试）
    print(f"[INFO] 开始执行任务：task_id={task_id}")
    if DEBUG_MODE:
        print(f"[DEBUG] target_dir={target_dir}")
        print(f"[DEBUG] prompt={prompt[:200]}...")
        print(f"[DEBUG] metadata={metadata}")

    async with semaphore:
        active_tasks += 1
        task_registry[task_id].status = "running"
        task_registry[task_id].started_at = get_local_datetime()

        # 更新数据库状态
        await save_task_to_db(task_registry[task_id])

        try:
            if not target_dir:
                target_dir = OUTPUT_ROOT
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
                if DEBUG_MODE:
                    print(f"[DEBUG] 创建/确认目录：{target_dir}")

            # 检查目录权限
            if os.access(target_dir, os.W_OK):
                if DEBUG_MODE:
                    print(f"[DEBUG] 目录可写：{target_dir}")
            else:
                print(f"[WARN] 目录不可写：{target_dir}")

            # 加载 CLAUDE.md
            claude_md_path = "/root/.claude/CLAUDE.md"
            if os.path.exists(claude_md_path):
                with open(claude_md_path, "r") as f:
                    claude_md_content = f.read()
                if DEBUG_MODE:
                    print(f"[DEBUG] 已加载 CLAUDE.md，长度：{len(claude_md_content)}")
                system_prompt = f"{system_prompt}\n\n## 项目规范\n{claude_md_content}"
            else:
                print(f"[WARN] CLAUDE.md 不存在：{claude_md_path}")

            stdout, stderr, result_target_dir = await run_claude_code_oneshot(
                prompt=prompt,
                target_dir=target_dir,
                system_prompt=system_prompt or "",
                auto_approve=auto_approve,
                skills=skills,
                task_id=task_id,
                timeout=CLAUDE_TIMEOUT,
            )

            # 检查是否有错误（stderr 非空说明 CLI 执行失败）
            if stderr and stderr.strip():
                print(f"[ERROR] 任务执行出错：{stderr[:500]}")
                task_registry[task_id].status = "failed"
                task_registry[task_id].error = stderr[:2000]  # 限制错误信息长度
            else:
                task_registry[task_id].status = "completed"

            # 构建 result，保留 metadata
            result = {
                "stdout": stdout,
                "stderr": stderr,
                "target_dir": result_target_dir,
            }
            if metadata:
                result["metadata"] = metadata

            task_registry[task_id].result = result
            task_registry[task_id].completed_at = get_local_datetime()

            # 从 stdout 中提取总结（markdown 格式）
            summary = extract_summary_from_stdout(stdout)
            if summary:
                task_registry[task_id].summary = summary
                if DEBUG_MODE:
                    print(f"[DEBUG] 提取总结成功，长度：{len(summary)} 字符")

            # 更新数据库状态
            await save_task_to_db(task_registry[task_id])

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].error = str(e)
            task_registry[task_id].completed_at = get_local_datetime()

            # 更新数据库状态
            await save_task_to_db(task_registry[task_id])

        finally:
            active_tasks -= 1


# ==================== Claude Code CLI 执行 ====================

async def run_claude_code_oneshot(
    prompt: str,
    target_dir: str,
    system_prompt: str,
    auto_approve: bool = True,
    skills: Optional[List[str]] = None,
    timeout: int = CLAUDE_TIMEOUT,
    task_id: Optional[str] = None,
) -> tuple:
    """运行 Claude Code CLI（一次性执行模式，自动授权）"""
    claude_cmd = await find_claude_command()

    if not claude_cmd:
        print("未找到 Claude Code CLI")
        return "", "Claude Code CLI 未安装"

    # 使用 -p (--print) 进行非交互式执行
    # 注意：不使用 --continue，因为它会恢复上一次会话的工作目录，导致 target_dir 无效
    cmd = [
        claude_cmd,
        "-p",  # 非交互式输出
        "--permission-mode", "dontAsk",  # 跳过权限确认
        "--allowed-tools", "Write,Bash,Read,Edit,Glob,Grep,WebFetch,WebSearch",
        "--output-format", "stream-json",  # 流式 JSON 输出
        "--verbose",  # verbose 模式（stream-json 必需）
    ]

    # 添加 prompt 作为位置参数（在 -p 之后）
    cmd.append(prompt)

    # 打印调试信息
    if DEBUG_MODE:
        print(f"[DEBUG] 执行 Claude 命令，target_dir={target_dir}")
        print(f"[DEBUG] 命令：{' '.join(cmd)}")
        print(f"[DEBUG] ANTHROPIC_BASE_URL={os.getenv('ANTHROPIC_BASE_URL', '未设置')}")
        print(f"[DEBUG] ANTHROPIC_MODEL={os.getenv('ANTHROPIC_MODEL', '未设置')}")
        print(f"[DEBUG] timeout={timeout}秒")

    env = os.environ.copy()
    if CLAUDE_API_KEY:
        env["CLAUDE_API_KEY"] = CLAUDE_API_KEY
    # 添加 API URL 和模型到环境变量
    if os.getenv("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.getenv("ANTHROPIC_BASE_URL")
    if os.getenv("ANTHROPIC_MODEL"):
        env["ANTHROPIC_MODEL"] = os.getenv("ANTHROPIC_MODEL")

    # 禁用交互式提示和 onboarding 流程
    env["CLAUDE_CODE_DISABLE_ANALYTICS"] = "1"
    env["CLAUDE_CODE_INTERACTIVE"] = "0"
    env["CLAUDE_CODE_SKIP_TELEMETRY"] = "1"

    # 确保 CLAUDE_CODE_WORKSPACE 设置正确
    env["CLAUDE_CODE_WORKSPACE"] = target_dir

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,  # 不接收输入，防止等待
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=target_dir,
        env=env,
    )

    if DEBUG_MODE:
        print(f"[DEBUG] 进程已启动，PID={process.pid}")

    system_message = f"{system_prompt}\n\n---\n\n{prompt}\n\n---\n\n请开始执行任务，不要等待确认，直接完成所有工作。"

    try:
        # 流式读取输出
        stdout_chunks = []
        stderr_chunks = []

        # 计数器，用于调试
        stdout_count = 0
        stderr_count = 0

        # 动态超时：跟踪最后一次活动时间
        last_activity_time = time.time()
        idle_timeout = timeout  # 无输出时的超时时间（使用配置的 CLAUDE_TIMEOUT）
        max_total_timeout = MAX_TOTAL_TIMEOUT  # 最大总执行时间（使用全局配置）

        async def read_stream(stream, output_type):
            nonlocal stdout_count, stderr_count, last_activity_time
            MAX_CHUNK_SIZE = 32768  # 最大输出块大小：32KB，超过则截断
            while True:
                try:
                    line = await asyncio.wait_for(stream.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    # 读取超时，继续循环检查
                    continue
                except ValueError as e:
                    # LimitOverrunError: 行太长，超过 readline 默认限制 (64KB)
                    if "chunk is longer than limit" in str(e):
                        try:
                            # 读取最大块大小，截断过长内容
                            chunk = await asyncio.wait_for(stream.read(MAX_CHUNK_SIZE), timeout=1.0)
                            if not chunk:
                                break
                            line = chunk
                            # 尝试读取并丢弃剩余的行内容，直到找到换行符
                            discard = await asyncio.wait_for(stream.readuntil(b'\n'), timeout=0.5)
                        except asyncio.TimeoutError:
                            # 无法找到换行符，跳过丢弃
                            pass
                        except:
                            # 其他读取错误，跳过丢弃
                            pass
                    else:
                        # 其他 ValueError，跳出循环
                        break
                if not line:
                    break
                # 截断过长的输出块
                chunk = line.decode(errors='replace')[:MAX_CHUNK_SIZE]
                last_activity_time = time.time()  # 更新活动时间
                if output_type == "stdout":
                    stdout_count += 1
                else:
                    stderr_count += 1
                stdout_chunks.append(chunk) if output_type == "stdout" else stderr_chunks.append(chunk)
                # 广播输出到 WebSocket
                if task_id:
                    await broadcast_output(task_id, output_type, chunk)

        # 打印调试信息
        if DEBUG_MODE:
            print(f"[DEBUG] 开始读取输出流，task_id={task_id}, idle_timeout={idle_timeout}s, max_total_timeout={max_total_timeout}s")

        # 检查超时的协程
        async def check_timeout():
            while True:
                await asyncio.sleep(1.0)
                current_time = time.time()
                idle_duration = current_time - last_activity_time
                total_duration = current_time - start_time

                # 检查空闲超时（无输出）
                if idle_duration >= idle_timeout:
                    print(f"[INFO] 空闲超时：{idle_duration:.1f}秒无输出")
                    return "idle_timeout"

                # 检查最大总超时
                if total_duration >= max_total_timeout:
                    print(f"[INFO] 最大执行时间超时：{total_duration:.1f}秒")
                    return "max_timeout"

        # 读取输出并监控超时
        start_time = time.time()

        async def read_with_timeout():
            timeout_task = asyncio.create_task(check_timeout())
            # asyncio.gather() 返回 Future，不需要 create_task 包装
            read_task = asyncio.gather(
                read_stream(process.stdout, "stdout"),
                read_stream(process.stderr, "stderr"),
            )

            done, pending = await asyncio.wait(
                {timeout_task, read_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 取消未完成的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # 检查是否是超时导致的结束
            if timeout_task in done:
                timeout_reason = timeout_task.result()
                raise TimeoutError(f"Claude Code 执行超时（空闲 {idle_timeout}秒无输出）")

        await read_with_timeout()

        # 等待进程结束
        await process.wait()

        # 检查返回码
        if process.returncode != 0:
            print(f"[WARN] Claude Code CLI 返回非零退出码：{process.returncode}")

        # 打印完成信息
        print(f"[INFO] 执行完成，task_id={task_id}, stdout={stdout_count}行，stderr={stderr_count}行")
        if DEBUG_MODE:
            print(f"[DEBUG] 已存储输出到 task_outputs: {len(task_outputs.get(task_id, []))}条")

        return "".join(stdout_chunks), "".join(stderr_chunks), target_dir

    except TimeoutError as e:
        # 动态超时触发的超时错误
        print(f"[WARN] 超时终止进程：{e}")
        process.kill()
        raise
    except Exception as e:
        # 其他异常也确保进程被终止
        process.kill()
        raise


async def find_claude_command() -> Optional[str]:
    """查找 Claude Code CLI 命令"""
    candidates = ["claude", "claude-code"]

    for cmd in candidates:
        try:
            process = await asyncio.create_subprocess_exec(
                "which", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if process.returncode == 0 and stdout.strip():
                return cmd
        except Exception:
            continue

    return None


# ==================== 工具函数 ====================

def extract_summary_from_stdout(stdout: str) -> Optional[str]:
    """
    从 Claude Code CLI 输出中提取总结。

    CLI 使用 --output-format stream-json 输出，最后一行包含 type: "result"，
    其中的 result 字段包含 markdown 格式的总结。
    """
    if not stdout or not stdout.strip():
        return None

    # 按行解析，找到最后一行 type: "result" 的 JSON
    lines = stdout.strip().split('\n')

    # 从后往前找 result 行
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get('type') == 'result' and obj.get('result'):
                return obj['result']
        except json.JSONDecodeError:
            continue

    return None


async def broadcast_output(task_id: str, output_type: str, data: str):
    """广播任务输出到所有连接的 WebSocket 并存储日志"""
    # 存储输出日志
    if task_id not in task_outputs:
        task_outputs[task_id] = []

    # 尝试解析 JSON 输出（stream-json 格式）- 用于提取 subagent_status
    subagent_status = None

    if output_type == "stdout" and data.strip().startswith("{"):
        try:
            json_data = json.loads(data.strip())
            # 提取有用信息
            if isinstance(json_data, dict):
                # 检测消息类型
                msg_type = json_data.get("type")
                role = json_data.get("role")

                # 根据消息类型/角色提取状态信息
                if msg_type == "system":
                    subagent_status = json_data.get("message", "")
                elif msg_type == "tool_use":
                    tool_name = json_data.get("name", "unknown")
                    subagent_status = f"正在调用工具：{tool_name}"
                elif msg_type == "tool_result":
                    subagent_status = "工具调用完成"
                elif role == "assistant":
                    # 助手消息 - 提取工具调用信息
                    tool_calls = json_data.get("tool_calls", [])
                    if tool_calls:
                        tool_names = [tc.get("name", "unknown") for tc in tool_calls]
                        subagent_status = f"调用工具：{', '.join(tool_names[:3])}"
                elif role == "user":
                    subagent_status = "处理用户输入"
        except json.JSONDecodeError:
            pass

    # 构建输出记录 - 发送原始数据，让前端解析
    output_record = {
        "type": "output",
        "source": output_type,
        "data": data,  # 发送原始 JSON 字符串，前端会自行解析
        "timestamp": get_local_datetime().isoformat(),
    }

    # 如果有子代理状态，添加到记录
    if subagent_status:
        output_record["subagent_status"] = subagent_status

    task_outputs[task_id].append(output_record)

    # 广播到 WebSocket
    if task_id in task_websockets:
        message = {
            "type": "output",
            "data": data,  # 发送原始数据
            "source": output_type,
        }
        if subagent_status:
            message["subagent_status"] = subagent_status
        for ws in task_websockets[task_id]:
            try:
                await ws.send_json(message)
            except Exception:
                pass  # 忽略发送失败的连接


async def save_task_to_db(task_record: TaskRecord, prompt: str = "", target_dir: str = "", system_prompt: str = ""):
    """保存任务到数据库"""
    if not AsyncSessionLocal:
        if DEBUG_MODE:
            print(f"[DEBUG] 数据库未初始化，任务仅保存在内存中：task_id={task_record.id}")
        return

    try:
        async with AsyncSessionLocal() as session:
            # 检查是否已存在
            from sqlalchemy import select
            result = await session.execute(
                select(TaskModel).where(TaskModel.task_id == task_record.id)
            )
            existing = result.scalar_one_or_none()

            if existing:
                # 更新
                existing.status = task_record.status
                existing.started_at = task_record.started_at
                existing.completed_at = task_record.completed_at
                existing.error = task_record.error
                existing.summary = task_record.summary  # 保存总结
                if task_record.result:
                    existing.result = json.dumps(task_record.result)
                if DEBUG_MODE:
                    print(f"[DEBUG] 更新数据库任务：task_id={task_record.id}, status={task_record.status}")
            else:
                # 新建
                new_task = TaskModel(
                    task_id=task_record.id,
                    task_type=task_record.task_type,
                    status=task_record.status,
                    prompt=prompt,
                    target_dir=target_dir,
                    system_prompt=system_prompt,
                    created_at=task_record.created_at,
                    started_at=task_record.started_at,
                    completed_at=task_record.completed_at,
                    error=task_record.error,
                    result=json.dumps(task_record.result) if task_record.result else None,
                    summary=task_record.summary,  # 保存总结
                )
                session.add(new_task)
                if DEBUG_MODE:
                    print(f"[DEBUG] 新建数据库任务：task_id={task_record.id}, prompt={prompt[:50]}...")

            await session.commit()
            if DEBUG_MODE:
                print(f"[DEBUG] 数据库提交成功：task_id={task_record.id}")

    except Exception as e:
        # 连接错误时尝试重新初始化
        error_str = str(e)
        if "connection is closed" in error_str or "connection failed" in error_str.lower():
            print(f"[ERROR] 数据库连接断开，尝试重新初始化：{e}")
            # 关闭旧引擎
            if db_engine:
                await db_engine.dispose()
            # 重新初始化数据库
            await init_database()
        else:
            print(f"[ERROR] 保存任务到数据库失败：task_id={task_record.id}, error={e}")


async def get_task_from_db(task_id: str) -> Optional[TaskModel]:
    """从数据库获取任务"""
    if not AsyncSessionLocal:
        return None

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(TaskModel).where(TaskModel.task_id == task_id)
            )
            return result.scalar_one_or_none()
    except Exception:
        return None


def verify_output(target_dir: str, max_size: int = 10 * 1024 * 1024) -> bool:
    """验证输出文件"""
    if not os.path.exists(target_dir):
        return False

    # 检查文件大小（最大 10MB）
    total_size = 0

    for root, dirs, files in os.walk(target_dir):
        for file in files:
            file_path = os.path.join(root, file)
            total_size += os.path.getsize(file_path)

    if total_size > max_size:
        print(f"输出文件过大：{total_size} bytes")
        return False

    return True


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn
    import sys

    # 配置日志格式（带时间戳，禁用默认访问日志）
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s - %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": sys.stderr,
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO"},
            "uvicorn.error": {"level": "INFO"},
            # 禁用 uvicorn 的默认访问日志（由中间件处理）
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
        },
    }

    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=log_config)
