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

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

# 数据库支持（可选）
try:
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import Column, Integer, String, DateTime, Float, Text, Index
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
        created_at = Column(Float, default=time.time)
        started_at = Column(Float, nullable=True)
        completed_at = Column(Float, nullable=True)

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
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[dict] = None
    error: Optional[str] = None


class StatusResponse(BaseModel):
    """服务状态响应"""
    pool_size: int
    active_tasks: int
    available_slots: int
    poll_enabled: bool


class TaskStatusResponse(BaseModel):
    """任务状态响应"""
    id: str
    task_type: str
    status: str
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None


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

async def init_database():
    """初始化数据库"""
    global db_engine, AsyncSessionLocal

    if not DATABASE_AVAILABLE or not DATABASE_URL:
        print("数据库未启用，任务将仅保存在内存中（重启后丢失）")
        return

    try:
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

            for task in tasks:
                # 恢复到内存 registry
                task_registry[task.task_id] = TaskRecord(
                    id=task.task_id,
                    task_type=task.task_type,
                    status=task.status,
                    created_at=task.created_at,
                    started_at=task.started_at,
                    completed_at=task.completed_at,
                    error=task.error,
                )

            print(f"从数据库恢复了 {len(tasks)} 个未完成的任务")

    except Exception as e:
        print(f"恢复任务失败：{e}")


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
        poll_enabled=TASK_POLL_ENABLED,
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
    if task_id not in task_registry:
        raise HTTPException(status_code=404, detail="任务不存在")
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


@app.post("/task", response_model=TaskResponse)
async def create_task(request: CustomTaskRequest, background_tasks: BackgroundTasks):
    """
    创建自定义任务（异步执行）

    任务会在后台执行，通过 /tasks/{task_id} 查询状态
    """
    task_id = request.task_id or str(uuid.uuid4())[:8]

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
    task_id = request.task_id or str(uuid.uuid4())[:8]

    task_registry[task_id] = TaskRecord(
        id=task_id,
        task_type="custom",
        status="pending",
    )

    async with semaphore:
        active_tasks += 1
        task_registry[task_id].status = "running"
        task_registry[task_id].started_at = time.time()

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
            )

            # 更新状态
            task_registry[task_id].status = "completed"
            task_registry[task_id].completed_at = time.time()
            task_registry[task_id].result = {
                "stdout": stdout,
                "stderr": stderr,
                "target_dir": target_dir,
            }

            return ExecuteResponse(
                success=True,
                message="任务执行完成",
                output=stdout[:2000] if stdout else None,
            )

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].completed_at = time.time()
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
    async with semaphore:
        active_tasks += 1
        task_registry[task_id].status = "running"
        task_registry[task_id].started_at = time.time()

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
            )

            task_registry[task_id].status = "completed"
            task_registry[task_id].result = {
                "stdout": stdout,
                "stderr": stderr,
                "target_dir": target_dir,
            }
            task_registry[task_id].completed_at = time.time()

            # 更新数据库状态
            await save_task_to_db(task_registry[task_id])

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].error = str(e)
            task_registry[task_id].completed_at = time.time()

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
) -> tuple:
    """运行 Claude Code CLI（一次性执行模式，自动授权）"""
    claude_cmd = await find_claude_command()

    if not claude_cmd:
        print("未找到 Claude Code CLI")
        return "", "Claude Code CLI 未安装"

    cmd = [
        claude_cmd,
        "--prompt", prompt,
        "--allowedTools", "Write,Bash,Read,Edit,Glob,Grep,WebFetch,WebSearch",
        "--outputFormat", "stream",
        "--continue",
    ]

    # 自动授权配置
    if auto_approve:
        if CLAUDE_AUTO_APPROVE == "all":
            cmd.append("--approve")
        elif CLAUDE_AUTO_APPROVE == "selective":
            cmd.extend(["--approve-tools", "Read,Write,Edit,Bash,Glob,Grep"])

    # 加载 skills
    if skills:
        for skill in skills:
            cmd.extend(["--skill", skill])

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
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=system_message.encode()),
            timeout=timeout,
        )
        return stdout.decode(), stderr.decode()
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

async def save_task_to_db(task_record: TaskRecord, prompt: str = "", target_dir: str = "", system_prompt: str = ""):
    """保存任务到数据库"""
    if not AsyncSessionLocal:
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

            await session.commit()

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
