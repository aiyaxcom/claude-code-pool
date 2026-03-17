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
from pathlib import Path
from typing import Optional, Dict, List, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# 数据库支持（可选）
try:
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import Column, Integer, String, DateTime, Text, Index
    from sqlalchemy.orm import declarative_base
    from datetime import datetime
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
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))

# Claude Code 配置
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_AUTO_APPROVE = os.getenv("CLAUDE_AUTO_APPROVE", "all")  # all, none, selective
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", "/workspace")
OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "/sites")

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
        error = Column(Text, nullable=True)
        created_at = Column(DateTime, default=datetime.utcnow)
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
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[dict] = None
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
    """迁移数据库 schema - 修复时间戳字段类型"""
    if not DATABASE_AVAILABLE or not DATABASE_URL:
        return

    try:
        db_engine = create_async_engine(DATABASE_URL, echo=False)

        # 检查是否需要迁移（检查 tasks 表的 started_at 字段类型）
        async with db_engine.begin() as conn:
            # 使用 raw SQL 检查列类型
            from sqlalchemy import text
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

                # 执行迁移
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
            else:
                print("[MIGRATE] 数据库 schema 已是最新，无需迁移")

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

        # 创建数据库引擎
        db_engine = create_async_engine(DATABASE_URL, echo=False)

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
                    # 恢复到内存
                    record = TaskRecord(
                        id=task.task_id,
                        task_type=task.task_type,
                        status=task.status,
                        created_at=task.created_at,
                        started_at=task.started_at,
                        completed_at=task.completed_at,
                        error=task.error,
                    )
                    task_registry[task_id] = record
                    print(f"[DEBUG] 从数据库恢复任务到内存：task_id={task_id}")

                    return TaskStatusResponse(
                        id=record.id,
                        task_type=record.task_type,
                        status=record.status,
                        created_at=record.created_at,
                        started_at=record.started_at,
                        completed_at=record.completed_at,
                        error=record.error,
                    )
        except Exception as e:
            print(f"[ERROR] 从数据库获取任务失败：{e}")

    raise HTTPException(status_code=404, detail="任务不存在")


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

    outputs = task_outputs.get(task_id, [])

    # 返回所有输出或只返回新增的输出
    if last_id > 0:
        outputs = outputs[last_id:]

    return {
        "task_id": task_id,
        "outputs": outputs,
        "total": len(task_outputs.get(task_id, []))
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

    print(f"[DEBUG] 创建任务：task_id={task_id}")

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
    )

    print(f"[DEBUG] 任务已创建并加入后台执行：task_id={task_id}")

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
        task_registry[task_id].started_at = datetime.utcnow()

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
            stdout, stderr = await run_claude_code_oneshot(
                prompt=request.prompt,
                target_dir=target_dir,
                system_prompt=system_prompt,
                auto_approve=request.auto_approve,
                skills=request.skills,
                task_id=task_id,
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

            task_registry[task_id].completed_at = datetime.utcnow()
            task_registry[task_id].result = {
                "stdout": stdout,
                "stderr": stderr,
                "target_dir": target_dir,
            }

            return ExecuteResponse(
                success=task_status == "completed",
                message="任务执行完成" if task_status == "completed" else f"任务执行失败：{stderr[:200]}",
                output=stdout[:2000] if stdout else None,
                error=stderr[:2000] if stderr else None,
            )

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].completed_at = datetime.utcnow()
            task_registry[task_id].error = str(e)
            return ExecuteResponse(
                success=False,
                message=f"任务执行失败：{str(e)}",
                error=str(e),
            )

        finally:
            active_tasks -= 1


# ==================== 任务执行器 ====================

async def execute_custom_task(
    task_id: str,
    prompt: str,
    target_dir: Optional[str],
    system_prompt: Optional[str],
    auto_approve: bool,
    skills: Optional[List[str]],
):
    """执行自定义任务（后台异步）"""
    global active_tasks

    async with semaphore:
        active_tasks += 1
        task_registry[task_id].status = "running"
        task_registry[task_id].started_at = datetime.utcnow()

        # 更新数据库状态
        await save_task_to_db(task_registry[task_id])

        try:
            if not target_dir:
                target_dir = OUTPUT_ROOT
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)

            # 加载 CLAUDE.md
            claude_md_path = "/root/.claude/CLAUDE.md"
            if os.path.exists(claude_md_path):
                with open(claude_md_path, "r") as f:
                    claude_md_content = f.read()
                system_prompt = f"{system_prompt}\n\n## 项目规范\n{claude_md_content}"

            stdout, stderr = await run_claude_code_oneshot(
                prompt=prompt,
                target_dir=target_dir,
                system_prompt=system_prompt or "",
                auto_approve=auto_approve,
                skills=skills,
                task_id=task_id,
            )

            # 检查是否有错误（stderr 非空说明 CLI 执行失败）
            if stderr and stderr.strip():
                print(f"[ERROR] 任务执行出错：{stderr[:500]}")
                task_registry[task_id].status = "failed"
                task_registry[task_id].error = stderr[:2000]  # 限制错误信息长度
            else:
                task_registry[task_id].status = "completed"

            task_registry[task_id].result = {
                "stdout": stdout,
                "stderr": stderr,
                "target_dir": target_dir,
            }
            task_registry[task_id].completed_at = datetime.utcnow()

            # 更新数据库状态
            await save_task_to_db(task_registry[task_id])

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].error = str(e)
            task_registry[task_id].completed_at = datetime.utcnow()

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
    cmd = [
        claude_cmd,
        "-p",  # 非交互式输出
        "--allowed-tools", "Write,Bash,Read,Edit,Glob,Grep,WebFetch,WebSearch",
        "--output-format", "stream-json",  # 流式 JSON 输出
        "--continue",
    ]

    # 添加 prompt 作为位置参数（在 -p 之后）
    cmd.append(prompt)

    # 注意：使用 -p 模式时自动跳过权限确认，不需要 --approve 参数
    # 自动授权配置已废弃
    # if auto_approve:
    #     if CLAUDE_AUTO_APPROVE == "all":
    #         cmd.append("--approve")
    #     elif CLAUDE_AUTO_APPROVE == "selective":
    #         cmd.extend(["--approve-tools", "Read,Write,Edit,Bash,Glob,Grep"])

    # 注意：新版 CLI 没有 --skill 参数
    # Skills 通过 settings.json 配置文件加载：
    # /root/.claude/settings.json 中的 "skills" 配置项
    # if skills:
    #     for skill in skills:
    #         cmd.extend(["--skill", skill])

    print(f"[OneShot] 执行 Claude 命令，target_dir={target_dir}")

    env = os.environ.copy()
    if CLAUDE_API_KEY:
        env["CLAUDE_API_KEY"] = CLAUDE_API_KEY

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=target_dir,
        env=env,
    )

    system_message = f"{system_prompt}\n\n---\n\n{prompt}\n\n---\n\n请开始执行任务，不要等待确认，直接完成所有工作。"

    try:
        # 流式读取输出
        stdout_chunks = []
        stderr_chunks = []

        async def read_stream(stream, output_type):
            while True:
                line = await stream.readline()
                if not line:
                    break
                chunk = line.decode()
                stdout_chunks.append(chunk) if output_type == "stdout" else stderr_chunks.append(chunk)
                # 广播输出到 WebSocket
                if task_id:
                    await broadcast_output(task_id, output_type, chunk)

        # 并发读取 stdout 和 stderr
        await asyncio.gather(
            read_stream(process.stdout, "stdout"),
            read_stream(process.stderr, "stderr"),
        )

        # 等待进程结束
        await process.wait()

        return "".join(stdout_chunks), "".join(stderr_chunks)

    except asyncio.TimeoutError:
        process.kill()
        raise TimeoutError(f"Claude Code 执行超时（{timeout}秒）")


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

async def broadcast_output(task_id: str, output_type: str, data: str):
    """广播任务输出到所有连接的 WebSocket 并存储日志"""
    # 存储输出日志
    if task_id not in task_outputs:
        task_outputs[task_id] = []
    task_outputs[task_id].append({
        "type": "output",
        "source": output_type,
        "data": data,
        "timestamp": datetime.utcnow().isoformat()
    })

    # 广播到 WebSocket
    if task_id in task_websockets:
        message = {
            "type": "output",
            "data": data,
            "source": output_type
        }
        for ws in task_websockets[task_id]:
            try:
                await ws.send_json(message)
            except Exception:
                pass  # 忽略发送失败的连接


async def save_task_to_db(task_record: TaskRecord, prompt: str = "", target_dir: str = "", system_prompt: str = ""):
    """保存任务到数据库"""
    if not AsyncSessionLocal:
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
                if task_record.result:
                    existing.result = json.dumps(task_record.result)
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
                )
                session.add(new_task)
                print(f"[DEBUG] 新建数据库任务：task_id={task_record.id}, prompt={prompt[:50]}...")

            await session.commit()
            print(f"[DEBUG] 数据库提交成功：task_id={task_record.id}")

    except Exception as e:
        print(f"[ERROR] 保存任务到数据库失败：task_id={task_record.id}, error={e}")

    except Exception as e:
        print(f"保存任务到数据库失败：{e}")


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
    uvicorn.run(app, host="0.0.0.0", port=8000)
