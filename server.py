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


# ==================== 配置 ====================

# 并发控制
POOL_SIZE = int(os.getenv("POOL_SIZE", "3"))
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))

# Claude Code 配置
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_AUTO_APPROVE = os.getenv("CLAUDE_AUTO_APPROVE", "all")  # all, none, selective
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", "/workspace")
OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "/sites")

# 主动轮询模式配置
TASK_POLL_ENABLED = os.getenv("TASK_POLL_ENABLED", "false").lower() == "true"
TASK_POLL_URL = os.getenv("TASK_POLL_URL", "")
TASK_POLL_INTERVAL = int(os.getenv("TASK_POLL_INTERVAL", "5"))
TASK_POLL_API_KEY = os.getenv("TASK_POLL_API_KEY", "")
TASK_POLL_CALLBACK_URL = os.getenv("TASK_POLL_CALLBACK_URL", "")

# 信号量控制并发
semaphore = asyncio.Semaphore(POOL_SIZE)

# 当前活跃的任务计数
active_tasks = 0

# 任务执行记录（用于跟踪状态）
task_registry: Dict[str, dict] = {}


# ==================== 数据模型 ====================

@dataclass
class TaskRecord:
    """任务执行记录"""
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    print(f"Claude Code Pool 启动，并发限制：{POOL_SIZE}")
    print(f"自动授权：{CLAUDE_AUTO_APPROVE}")
    print(f"主动轮询：{TASK_POLL_ENABLED}")

    if TASK_POLL_ENABLED and TASK_POLL_URL:
        print(f"轮询 URL: {TASK_POLL_URL}")
        print(f"轮询间隔：{TASK_POLL_INTERVAL}秒")
        # 启动后台轮询任务
        asyncio.create_task(poll_loop())

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


# ==================== 主动轮询模式 ====================

async def poll_loop():
    """主动轮询任务队列"""
    print(f"启动轮询循环：每{TASK_POLL_INTERVAL}秒轮询一次")

    while TASK_POLL_ENABLED:
        try:
            await poll_and_process_task()
        except Exception as e:
            print(f"轮询失败：{e}")

        await asyncio.sleep(TASK_POLL_INTERVAL)


async def poll_and_process_task():
    """轮询并处理单个任务"""
    if active_tasks >= POOL_SIZE:
        print("并发任务数已达上限，跳过本次轮询")
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {}
        if TASK_POLL_API_KEY:
            headers["Authorization"] = f"Bearer {TASK_POLL_API_KEY}"

        try:
            # 获取待处理任务
            response = await client.get(TASK_POLL_URL, headers=headers)
            response.raise_for_status()

            task_data = response.json()

            # 如果没有待处理任务，直接返回
            if not task_data or task_data.get("status") != "pending":
                return

            # 处理任务
            await process_external_task(task_data)

        except httpx.HTTPError as e:
            print(f"轮询请求失败：{e}")
        except Exception as e:
            print(f"处理任务失败：{e}")


async def process_external_task(task_data: dict):
    """处理外部任务"""
    task_id = task_data.get("id", str(uuid.uuid4())[:8])
    task_type = task_data.get("type", "custom")
    prompt = task_data.get("prompt", "")
    target_dir = task_data.get("target_dir")
    system_prompt = task_data.get("system_prompt", "")
    skills = task_data.get("skills")
    metadata = task_data.get("metadata", {})

    # 创建任务记录
    task_registry[task_id] = TaskRecord(
        id=task_id,
        task_type=task_type,
        status="pending",
        result={"external_data": task_data},
    )

    async with semaphore:
        active_tasks += 1
        task_registry[task_id].status = "running"
        task_registry[task_id].started_at = time.time()

        try:
            # 确定目标目录
            if not target_dir:
                target_dir = os.path.join(OUTPUT_ROOT, f"task-{task_id}")

            os.makedirs(target_dir, exist_ok=True)

            # 加载 CLAUDE.md
            claude_md_path = "/root/.claude/CLAUDE.md"
            if os.path.exists(claude_md_path):
                with open(claude_md_path, "r") as f:
                    claude_md_content = f.read()
                system_prompt = f"{system_prompt}\n\n## 项目规范\n{claude_md_content}"

            # 执行任务
            stdout, stderr = await run_claude_code_oneshot(
                prompt=prompt,
                target_dir=target_dir,
                system_prompt=system_prompt,
                auto_approve=True,
                skills=skills,
            )

            # 更新状态
            task_registry[task_id].status = "completed"
            task_registry[task_id].completed_at = time.time()
            task_registry[task_id].result = {
                "stdout": stdout,
                "stderr": stderr,
                "target_dir": target_dir,
                "metadata": metadata,
            }

            # 回调通知
            await notify_task_completion(task_id, "completed")

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].completed_at = time.time()
            task_registry[task_id].error = str(e)
            await notify_task_completion(task_id, "failed", str(e))

        finally:
            active_tasks -= 1


async def notify_task_completion(task_id: str, status: str, error: Optional[str] = None):
    """通知外部 API 任务完成"""
    if not TASK_POLL_CALLBACK_URL:
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {}
            if TASK_POLL_API_KEY:
                headers["Authorization"] = f"Bearer {TASK_POLL_API_KEY}"

            payload = {
                "task_id": task_id,
                "status": status,
                "error": error,
                "result": task_registry[task_id].result,
            }

            await client.post(
                TASK_POLL_CALLBACK_URL,
                json=payload,
                headers=headers,
            )
            print(f"已通知任务完成：{task_id} -> {status}")

    except Exception as e:
        print(f"通知任务完成失败：{e}")


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

        except Exception as e:
            task_registry[task_id].status = "failed"
            task_registry[task_id].error = str(e)

        finally:
            active_tasks -= 1
            task_registry[task_id].completed_at = time.time()


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
