import os
import json
import uuid
import shutil
from datetime import datetime
from credit_risk_control.config import (
    TASK_PENDING_DIR,
    TASK_DONE_DIR,
    AUDIT_LOG_DIR,
)
from credit_risk_control import audit_log as audit


def _ensure_dirs():
    os.makedirs(TASK_PENDING_DIR, exist_ok=True)
    os.makedirs(TASK_DONE_DIR, exist_ok=True)


def _make_task_id(task_type: str) -> str:
    return f"TASK-{task_type.upper()}-{uuid.uuid4().hex[:8].upper()}"


def _load_all_audit_entries() -> list:
    if not os.path.exists(AUDIT_LOG_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(AUDIT_LOG_DIR)):
        if not fname.startswith("audit_"):
            continue
        fpath = os.path.join(AUDIT_LOG_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                entries = json.load(f)
                results.extend(entries)
        except (json.JSONDecodeError, OSError):
            continue
    return results


def submit_task(task_type: str, payload: dict, operator: str = "用户", parent_task_id: str = None) -> dict:
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
        "started_at": None,
        "parent_task_id": parent_task_id,
    }
    filepath = os.path.join(TASK_PENDING_DIR, f"{task_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    detail_parts = [f"任务 [{task_type}] 提交到队列"]
    if parent_task_id:
        detail_parts.append(f"重试来源: {parent_task_id}")
    audit.log(
        action="提交任务",
        operator=operator,
        target_type="任务队列",
        target_id=task_id,
        detail=", ".join(detail_parts),
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
            if task.get("status") in ("pending", "running"):
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
    if error:
        task["status"] = "failed"
    elif result and result.get("_business_failed"):
        task["status"] = "failed"
        task["error"] = result.get("_failure_reason", "业务流程失败")
        task["result"] = result
        task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        task["status"] = "done"
        task["result"] = result
        task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if task["status"] == "failed" and not task.get("error"):
        task["error"] = error or "未知错误"

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

    is_failed = task["status"] == "failed"
    audit.log(
        action="任务失败" if is_failed else "任务完成",
        operator=task.get("operator", "系统"),
        target_type="任务队列",
        target_id=task["task_id"],
        detail=(
            f"任务 [{task['type']}] 状态: {task['status']}, "
            f"结果: {str(result)[:80] if result else '无'}, "
            f"错误: {task.get('error') or '无'}"
        ),
    )
    return task


def submit_publish_task(strategy_id: str, operator: str = "用户", parent_task_id: str = None) -> dict:
    return submit_task(
        "publish",
        {"strategy_id": strategy_id, "approval_mode": "auto", "monitor": False},
        operator,
        parent_task_id=parent_task_id,
    )


def submit_manual_report_task(operator: str = "用户", parent_task_id: str = None) -> dict:
    return submit_task("manual_report", {}, operator, parent_task_id=parent_task_id)


def list_tasks(
    status_filter: str = None,
    task_type: str = None,
    operator: str = None,
    strategy_id: str = None,
    time_start: str = None,
    time_end: str = None,
) -> list:
    _ensure_dirs()
    all_tasks = []

    for directory in (TASK_PENDING_DIR, TASK_DONE_DIR):
        if not os.path.exists(directory):
            continue
        for fname in sorted(os.listdir(directory), reverse=True):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(directory, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    task = json.load(f)
                task["_filepath"] = fpath
                all_tasks.append(task)
            except (json.JSONDecodeError, OSError):
                continue

    filtered = []
    for t in all_tasks:
        if status_filter and t.get("status") != status_filter:
            continue
        if task_type and t.get("type") != task_type:
            continue
        if operator and t.get("operator") != operator:
            continue
        if strategy_id:
            payload_sid = t.get("payload", {}).get("strategy_id", "")
            result_sid = (t.get("result") or {}).get("strategy_id", "")
            if strategy_id not in (payload_sid, result_sid):
                continue
        if time_start and t.get("submitted_at", "") < time_start:
            continue
        if time_end and t.get("submitted_at", "") > time_end:
            continue
        filtered.append(t)

    return filtered


def get_task_detail(task_id: str) -> dict:
    _ensure_dirs()
    for directory in (TASK_PENDING_DIR, TASK_DONE_DIR):
        if not os.path.exists(directory):
            continue
        fpath = os.path.join(directory, f"{task_id}.json")
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    task = json.load(f)
                task["_filepath"] = fpath
                return task
            except (json.JSONDecodeError, OSError):
                return None
    return None


def cancel_task(task_id: str) -> dict:
    fpath = os.path.join(TASK_PENDING_DIR, f"{task_id}.json")
    if not os.path.exists(fpath):
        return {"success": False, "error": f"任务 {task_id} 不存在于待处理队列中"}

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"success": False, "error": "任务文件读取失败"}

    if task.get("status") == "running":
        return {"success": False, "error": f"任务 {task_id} 正在运行中，不可取消"}

    task["status"] = "cancelled"
    task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    dest_path = os.path.join(TASK_DONE_DIR, f"{task_id}.json")
    try:
        shutil.move(fpath, dest_path)
    except OSError:
        pass

    audit.log(
        action="取消任务",
        operator=task.get("operator", "用户"),
        target_type="任务队列",
        target_id=task_id,
        detail=f"任务 [{task.get('type')}] 已取消",
    )
    return {"success": True, "task": task}


def retry_task(task_id: str, operator: str = None) -> dict:
    detail = get_task_detail(task_id)
    if not detail:
        return {"success": False, "error": f"任务 {task_id} 不存在"}

    if detail.get("status") != "failed":
        return {"success": False, "error": f"任务 {task_id} 状态为 {detail.get('status')}，只有失败任务可以重试"}

    op = operator or detail.get("operator", "用户")
    new_task = submit_task(
        task_type=detail["type"],
        payload=detail.get("payload", {}),
        operator=op,
        parent_task_id=task_id,
    )
    return {"success": True, "new_task": new_task, "original_task_id": task_id}


def get_task_audit_logs(task_id: str) -> list:
    all_entries = _load_all_audit_entries()
    return [e for e in all_entries if e.get("target_id") == task_id]
