# -*- coding: utf-8 -*-
"""Claude Code bridge: lets the AIRI digital human (Kimi LLM) create and drive
Claude Code sessions via the Agent SDK.

Endpoints:
  POST /run                        Start a task {prompt, cwd_key} -> {task_id}
  GET  /tasks/{id}/events          SSE stream: log/tool/text/waiting_input/done
  POST /tasks/{id}/answer          Answer a pending question {request_id, behavior, answers|message}
  GET  /tasks/{id}                 Status snapshot
  GET  /health                     Health check

Config: config.json next to this file (auto-created with defaults).
Claude Code login state is inherited from the user profile; claude.exe must be
on PATH (see claude_bin_dir below).
"""
import asyncio
import json
import os
import sys
import time
import uuid

# The SDK prints model output (may contain emoji) to stdout; on GBK consoles
# that raises UnicodeEncodeError and kills the task. Force UTF-8 on stdio.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# claude.exe lives here on this machine; the service process PATH may not include it.
os.environ["PATH"] = r"C:\Users\tgv1\.local\bin;" + os.environ.get("PATH", "")

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "port": 8932,
    "model": "",                 # empty = Claude Code default model
    "max_turns": 50,
    "permission_mode": "acceptEdits",
    "claude_bin_dir": r"C:\Users\tgv1\.local\bin",
    "cwd_whitelist": {
        "digital-human": r"F:\digital-human",
        "kps": r"D:\MyRobot\N_KPS_Arm",
    },
}


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


CONFIG = _load_config()
if CONFIG.get("claude_bin_dir"):
    os.environ["PATH"] = CONFIG["claude_bin_dir"] + os.pathsep + os.environ["PATH"]

app = FastAPI(title="Claude Code Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now():
    return time.strftime("%H:%M:%S")


def _log(msg):
    print(f"[{_now()}] {msg}", flush=True)


# ---------------------------------------------------------------- tasks state
class Task:
    def __init__(self, task_id, prompt, cwd_key):
        self.id = task_id
        self.prompt = prompt
        self.cwd_key = cwd_key
        self.queue = asyncio.Queue()
        self.pending = {}          # request_id -> {future, tool_name, input}
        self.status = "running"    # running / waiting / done / error
        self.result = ""
        self.error = None
        self.started_at = time.time()

    async def emit(self, ev):
        await self.queue.put(ev)


TASKS = {}   # task_id -> Task
TASKS_LOCK = asyncio.Lock()


# ---------------------------------------------------------------- claude sdk
async def _dummy_hook(input_data, tool_use_id, context):
    return {"continue_": True}


def _format_preview(tool_name, input_data):
    """Human-readable one-line summary of a permission request (for display
    and TTS); falls back to raw JSON for unknown tools."""
    if not isinstance(input_data, dict):
        return str(input_data)[:300]
    if tool_name == "AskUserQuestion":
        try:
            qs = input_data.get("questions", [])
            return " | ".join(q.get("question", "") for q in qs)
        except Exception:
            return ""
    desc = str(input_data.get("description") or "").strip()
    if tool_name in ("Bash", "PowerShell", "Cmd"):
        cmd = str(input_data.get("command") or "").strip()
        preview = f"请求执行 {tool_name} 命令：{cmd}"
        if desc:
            preview += f"（用途：{desc}）"
        return preview[:300]
    if tool_name in ("Write", "Edit", "NotebookEdit", "MultiEdit", "Read",
                     "Glob", "Grep", "LS"):
        path = str(input_data.get("file_path")
                   or input_data.get("path")
                   or input_data.get("pattern") or "").strip()
        if path:
            verb = "读取" if tool_name in ("Read", "Glob", "Grep", "LS") else "修改"
            preview = f"{verb}文件：{path}"
            if desc:
                preview += f"（{desc}）"
            return preview[:300]
    if desc:
        return f"{tool_name}：{desc}"[:300]
    return json.dumps(input_data, ensure_ascii=False)[:300]


async def _run_task(task: Task):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query

    async def can_use_tool(tool_name, input_data, context):
        request_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        task.pending[request_id] = {"future": future, "loop": loop,
                                    "tool_name": tool_name, "input": input_data,
                                    "preview": ""}
        task.status = "waiting"
        preview = _format_preview(tool_name, input_data)
        task.pending[request_id]["preview"] = preview
        await task.emit({"type": "waiting_input", "request_id": request_id,
                         "tool_name": tool_name, "preview": preview})
        _log(f"task={task.id} 提问挂起 tool={tool_name}")
        result = await future
        task.pending.pop(request_id, None)
        task.status = "running"
        return result

    cwd = CONFIG["cwd_whitelist"][task.cwd_key]
    option_kwargs = dict(
        cwd=cwd,
        max_turns=CONFIG.get("max_turns", 50),
        permission_mode=CONFIG.get("permission_mode", "acceptEdits"),
        can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_dummy_hook])]},
    )
    if CONFIG.get("model"):
        option_kwargs["model"] = CONFIG["model"]
    options = ClaudeAgentOptions(**option_kwargs)

    final_text = ""
    try:
        async for msg in query(prompt=task.prompt, options=options):
            mtype = type(msg).__name__
            if mtype == "AssistantMessage":
                for block in msg.content:
                    btype = getattr(block, "type", "")
                    if btype == "text":
                        final_text = block.text
                        await task.emit({"type": "text", "text": block.text})
                    elif btype == "thinking":
                        await task.emit({"type": "log", "level": "think",
                                         "text": getattr(block, "thinking", "")[:200]})
                    elif btype == "tool_use":
                        inp = json.dumps(getattr(block, "input", {}), ensure_ascii=False)[:200]
                        await task.emit({"type": "tool_start", "tool": block.name, "input": inp})
                        _log(f"task={task.id} tool={block.name}")
            elif mtype == "UserMessage":
                content = getattr(msg, "content", None)
                if isinstance(content, str):
                    pass  # raw user echo; ignore
            elif mtype == "ResultMessage":
                task.result = getattr(msg, "result", "") or final_text
                task.status = "done"
                await task.emit({"type": "done", "result": task.result,
                                 "is_error": bool(getattr(msg, "is_error", False))})
                _log(f"task={task.id} 完成 result={task.result[:50]}")
    except Exception as e:
        task.status = "error"
        task.error = f"{type(e).__name__}: {e}"
        await task.emit({"type": "error", "message": task.error})
        _log(f"task={task.id} 出错: {task.error}")


# ---------------------------------------------------------------- http api
class RunRequest(BaseModel):
    prompt: str
    cwd_key: str = "digital-human"


class AnswerRequest(BaseModel):
    request_id: str
    behavior: str = "allow"          # allow | deny
    message: str = ""                # deny reason (shown to Claude)
    answers: dict | None = None      # AskUserQuestion: {question_text: option_label}


@app.get("/health")
def health():
    return {"status": "ok", "tasks": len(TASKS),
            "cwd_whitelist": list(CONFIG["cwd_whitelist"].keys())}


FORMAT_SUFFIX = (
    "\n\n【回复格式要求】你的最终回复必须严格分成两部分，用单独一行 --- 分隔："
    "第一部分「语音摘要」：一两句口语化的话概括关键结果（这部分会被语音朗读给用户，"
    "不能包含代码、命令输出、文件路径列表）；"
    "第二部分「详细内容」：完整结果、命令输出、代码、文件路径等，供用户在屏幕上阅读"
    "（这部分不会被朗读）。先写语音摘要，再写一行 ---，再写详细内容。"
)


@app.post("/run")
async def run(req: RunRequest):
    if not req.prompt.strip():
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    if req.cwd_key not in CONFIG["cwd_whitelist"]:
        return JSONResponse({"error": f"unknown cwd_key: {req.cwd_key}",
                             "allowed": list(CONFIG["cwd_whitelist"].keys())}, status_code=400)
    task_id = uuid.uuid4().hex[:12]
    task = Task(task_id, req.prompt.strip() + FORMAT_SUFFIX, req.cwd_key)
    async with TASKS_LOCK:
        TASKS[task_id] = task
        if len(TASKS) > 50:      # bound memory: drop oldest finished tasks
            for tid in [t.id for t in TASKS.values() if t.status in ("done", "error")][:10]:
                TASKS.pop(tid, None)
    asyncio.create_task(_run_task(task))
    _log(f"task={task_id} 启动 cwd={req.cwd_key} prompt={req.prompt[:40]}")
    return {"task_id": task_id}


@app.get("/tasks")
async def list_tasks():
    async with TASKS_LOCK:
        items = sorted(TASKS.values(), key=lambda t: t.started_at, reverse=True)
    return {"tasks": [{
        "task_id": t.id, "status": t.status, "cwd_key": t.cwd_key,
        "pending": len(t.pending),
        "preview": next((p.get("preview", "") for p in t.pending.values()), ""),
        "started_at": time.strftime("%H:%M:%S", time.localtime(t.started_at)),
        "elapsed_s": round(time.time() - t.started_at, 1),
        "result": (t.result or "")[:80],
    } for t in items[:20]]}


@app.get("/tasks/{task_id}")
async def task_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return {"task_id": task_id, "status": task.status, "cwd_key": task.cwd_key,
            "pending": [{"request_id": rid, "tool_name": p["tool_name"],
                         "input": p["input"], "preview": p.get("preview", "")}
                        for rid, p in task.pending.items()],
            "result": task.result, "error": task.error,
            "elapsed_s": round(time.time() - task.started_at, 1)}


@app.get("/tasks/{task_id}/events")
async def task_events(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")

    async def stream():
        yield f"data: {json.dumps({'type': 'hello', 'task_id': task_id}, ensure_ascii=False)}\n\n"
        while True:
            try:
                ev = await asyncio.wait_for(task.queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") in ("done", "error"):
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/tasks/{task_id}/answer")
async def answer(task_id: str, req: AnswerRequest):
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    pending = task.pending.get(req.request_id)
    if not pending:
        raise HTTPException(404, f"no pending request {req.request_id}")
    future = pending["future"]
    if future.done():
        raise HTTPException(409, "request already answered")

    if req.behavior == "deny":
        result = PermissionResultDeny(message=req.message or "用户拒绝了该操作")
    elif pending["tool_name"] == "AskUserQuestion":
        answers = req.answers or {}
        questions = pending["input"].get("questions", [])
        updated = {"questions": questions, "answers": {}}
        for q in questions:
            qtext = q.get("question", "")
            updated["answers"][qtext] = answers.get(qtext, answers.get("*", ""))
        result = PermissionResultAllow(updated_input=updated)
    else:
        result = PermissionResultAllow()
    future.set_result(result)
    _log(f"task={task_id} 答复 request={req.request_id} behavior={req.behavior}")
    return {"ok": True}


if __name__ == "__main__":
    _log(f"Claude Code bridge starting on :{CONFIG['port']}, whitelist={list(CONFIG['cwd_whitelist'].keys())}")
    uvicorn.run(app, host="127.0.0.1", port=CONFIG["port"], log_level="warning")