import os
import json
import uuid
from datetime import datetime
from credit_risk_control import AuditLogEntry
from credit_risk_control.config import AUDIT_LOG_DIR


def _ensure_dir():
    os.makedirs(AUDIT_LOG_DIR, exist_ok=True)


def log(action: str, operator: str, target_type: str, target_id: str, detail: str) -> AuditLogEntry:
    _ensure_dir()
    entry = AuditLogEntry(
        log_id=f"AUD-{uuid.uuid4().hex[:8].upper()}",
        action=action,
        operator=operator,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    )
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(AUDIT_LOG_DIR, f"audit_{date_str}.json")
    entries = []
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                entries = []
    entries.append({
        "log_id": entry.log_id,
        "action": entry.action,
        "operator": entry.operator,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "detail": entry.detail,
        "timestamp": entry.timestamp,
    })
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    return entry


def query_logs(date: str = None, action: str = None, target_id: str = None) -> list:
    _ensure_dir()
    results = []
    if date:
        files = [os.path.join(AUDIT_LOG_DIR, f"audit_{date}.json")]
    else:
        files = sorted(
            [os.path.join(AUDIT_LOG_DIR, f) for f in os.listdir(AUDIT_LOG_DIR) if f.startswith("audit_")]
        )

    for log_file in files:
        if not os.path.exists(log_file):
            continue
        with open(log_file, "r", encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                continue
        for entry in entries:
            if action and entry.get("action") != action:
                continue
            if target_id and entry.get("target_id") != target_id:
                continue
            results.append(entry)
    return results
