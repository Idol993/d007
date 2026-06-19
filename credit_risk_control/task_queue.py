import os
import json
import uuid
import time
import shutil
from datetime import datetime
from credit_risk_control.config import (
    TASK_PENDING_DIR,
    TASK_DONE_DIR,
)
from credit_risk_control import audit_log as audit


def _ensure_dirs():
    os.makedirs(TASK_PENDING_DIR, exist_ok=True)
    os.makedirs(TASK_DONE_DIR, exist_ok=True)


def _make_task_id(task_type: str) -> str:
    return f"TASK-{task_type.upper()}-{uuid.uuid4().hex[:8].upper()}"


def submit_task(task_type: str, payload: dict, operator: str = "用户") -> dict:
    _ensure_dirs()
    task_id = _make_task_id(task_type)
    task = {
        "task_id": task_id,
        "type": task_type,
        "payload": payload,
        "operator": operator,
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
        "result": None,
        "error": None,
        "completed_at": None,
    }
    filepath = os.path.join(TASK_PENDING_DIR, f"{task_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    audit.log(
        action="提交任务",
        operator=operator,
        target_type="任务队列",
        target_id=task_id,
        detail=f"任务 [{task_type}] 提交到队列，文件: {filepath}",
    )
    return task


def poll_pending_tasks() -> list:
    _ensure_dirs()
    tasks = []
    for fname in sorted(os.listdir(TASK_PENDING_DIR)):
        if not fname.endswith(".json"):
            continue
        filepath = os.path.join(TASK_PENDING_DIR, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                task = json.load(f)
            if task.get("status") == "pending":
                tasks.append((filepath, task))
        except (json.JSONDecodeError, OSError):
            continue
    return tasks


def mark_task_running(filepath: str, task: dict) -> dict:
    task["status"] = "running"
    task["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    return task


def complete_task(filepath: str, task: dict, result: dict = None, error: str = None) -> dict:
    task["status"] = "failed" if error else "done"
    task["result"] = result
    task["error"] = error
    task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    _ensure_dirs()
    fname = os.path.basename(filepath)
    dest_path = os.path.join(TASK_DONE_DIR, fname)
    try:
        shutil.move(filepath, dest_path)
    except OSError:
        try:
            os.remove(filepath)
        except OSError:
            pass

    audit.log(
        action="任务完成" if not error else "任务失败",
        operator=task.get("operator", "系统"),
        target_type="任务队列",
        target_id=task["task_id"],
        detail=f"任务 [{task['type']}] 状态: {task['status']}, 结果: {str(result)[:80] if result else '无'}, 错误: {error if error else '无'}",
    )
    return task


def submit_publish_task(strategy_id: str, operator: str = "用户") -> dict:
    return submit_task(
        "publish",
        {"strategy_id": strategy_id, "approval_mode": "auto", "monitor": False},
        operator,
    )


def submit_manual_report_task(operator: str = "用户") -> dict:
    return submit_task("manual_report", {}, operator)
