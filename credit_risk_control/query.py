import json
import os
import csv
import zipfile
import io
from datetime import datetime
from credit_risk_control.config import (
    PUBLISH_RECORDS_FILE,
    ROLLBACK_RECORDS_FILE,
    APPROVAL_FLOWS_FILE,
    AUDIT_LOG_DIR,
    EXPORT_DIR,
)
from credit_risk_control import audit_log as audit


def _load_json(filepath: str) -> list:
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _load_all_audit_logs(time_start: str = None, time_end: str = None) -> list:
    if not os.path.exists(AUDIT_LOG_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(AUDIT_LOG_DIR)):
        if not fname.startswith("audit_"):
            continue
        date_str = fname.replace("audit_", "").replace(".json", "")
        if time_start and date_str < time_start[:10]:
            continue
        if time_end and date_str > time_end[:10]:
            continue
        fpath = os.path.join(AUDIT_LOG_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                entries = json.load(f)
                results.extend(entries)
        except (json.JSONDecodeError, OSError):
            continue
    return results


def query_publish_records(
    publish_time_start: str = None,
    publish_time_end: str = None,
    credit_product: str = None,
    customer_segment: str = None,
    version: str = None,
) -> list:
    records = _load_json(PUBLISH_RECORDS_FILE)
    filtered = []

    for r in records:
        if publish_time_start and r.get("publish_time", "") < publish_time_start:
            continue
        if publish_time_end and r.get("publish_time", "") > publish_time_end:
            continue
        if credit_product and r.get("credit_product") != credit_product:
            continue
        if customer_segment and r.get("customer_segment") != customer_segment:
            continue
        if version and version not in r.get("version", ""):
            continue
        filtered.append(r)

    audit.log(
        action="查询发布记录",
        operator="用户",
        target_type="发布记录",
        target_id="查询",
        detail=(
            f"查询条件: 时间[{publish_time_start}~{publish_time_end}], "
            f"产品[{credit_product}], 客群[{customer_segment}], 版本[{version}], "
            f"结果数: {len(filtered)}"
        ),
    )

    return filtered


def _write_csv(rows: list, headers: list = None) -> str:
    buf = io.StringIO()
    if not rows:
        if headers:
            writer = csv.DictWriter(buf, fieldnames=headers)
            writer.writeheader()
        return buf.getvalue()
    fieldnames = headers or list(rows[0].keys())
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        if isinstance(r, dict):
            writer.writerow(r)
    return buf.getvalue()


def batch_export(
    records: list,
    export_format: str = "csv",
    publish_time_start: str = None,
    publish_time_end: str = None,
) -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    strategy_ids = list({r.get("strategy_id") for r in records if r.get("strategy_id")})

    rollback_records = [
        r for r in _load_json(ROLLBACK_RECORDS_FILE)
        if r.get("strategy_id") in strategy_ids
    ]

    approval_flows = [
        f for f in _load_json(APPROVAL_FLOWS_FILE)
        if f.get("strategy_id") in strategy_ids
    ]

    audit_entries = _load_all_audit_logs(publish_time_start, publish_time_end)
    relevant_audit = [
        e for e in audit_entries
        if e.get("target_id") in strategy_ids
        or (publish_time_start and publish_time_end and publish_time_start <= e.get("timestamp", "") <= publish_time_end)
    ]

    if export_format == "json":
        filename = f"compliance_export_{timestamp}.zip"
        filepath = os.path.join(EXPORT_DIR, filename)
        with zipfile.ZipFile(filepath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("publish_records.json", json.dumps(records, ensure_ascii=False, indent=2))
            zf.writestr("rollback_records.json", json.dumps(rollback_records, ensure_ascii=False, indent=2))
            zf.writestr("approval_flows.json", json.dumps(approval_flows, ensure_ascii=False, indent=2))
            zf.writestr("audit_logs.json", json.dumps(relevant_audit, ensure_ascii=False, indent=2))
            readme = (
                "合规留档导出说明\n"
                "================\n"
                f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"筛选条件: 时间[{publish_time_start}~{publish_time_end}]\n"
                f"导出内容:\n"
                f"  - publish_records.json: 发布记录 {len(records)} 条\n"
                f"  - rollback_records.json: 回滚记录 {len(rollback_records)} 条\n"
                f"  - approval_flows.json: 审批记录 {len(approval_flows)} 条\n"
                f"  - audit_logs.json: 审计日志 {len(relevant_audit)} 条\n"
            )
            zf.writestr("README.txt", readme)
    else:
        filename = f"compliance_export_{timestamp}.zip"
        filepath = os.path.join(EXPORT_DIR, filename)
        with zipfile.ZipFile(filepath, "w", zipfile.ZIP_DEFLATED) as zf:
            publish_headers = [
                "record_id", "strategy_id", "strategy_name", "version",
                "risk_level", "credit_product", "customer_segment",
                "status", "publish_time", "operator", "rollback_count",
            ]
            zf.writestr("publish_records.csv", _write_csv(records, publish_headers))

            rollback_headers = [
                "rollback_id", "strategy_id", "strategy_name", "strategy_version",
                "reason", "trigger_metric", "trigger_value", "threshold_value",
                "affected_segments", "compliance_risk_desc", "previous_stable_version",
                "status", "created_at", "notified_roles",
            ]
            zf.writestr("rollback_records.csv", _write_csv(rollback_records, rollback_headers))

            approval_rows = []
            for f in approval_flows:
                for step in f.get("steps", []):
                    approval_rows.append({
                        "flow_id": f["flow_id"],
                        "strategy_id": f["strategy_id"],
                        "risk_level": f.get("risk_level"),
                        "flow_status": f.get("status"),
                        "step_order": step.get("step_order"),
                        "role": step.get("role"),
                        "approver": step.get("approver"),
                        "step_status": step.get("status"),
                        "comment": step.get("comment"),
                        "approved_at": step.get("approved_at"),
                    })
            approval_headers = [
                "flow_id", "strategy_id", "risk_level", "flow_status",
                "step_order", "role", "approver", "step_status",
                "comment", "approved_at",
            ]
            zf.writestr("approval_records.csv", _write_csv(approval_rows, approval_headers))

            audit_headers = [
                "log_id", "action", "operator", "target_type",
                "target_id", "detail", "timestamp",
            ]
            zf.writestr("audit_logs.csv", _write_csv(relevant_audit, audit_headers))

            readme = (
                "合规留档导出说明\n"
                "================\n"
                f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"筛选条件: 时间[{publish_time_start}~{publish_time_end}]\n"
                f"导出内容 (CSV格式, UTF-8编码):\n"
                f"  - publish_records.csv: 发布记录 {len(records)} 条\n"
                f"  - rollback_records.csv: 回滚记录 {len(rollback_records)} 条\n"
                f"  - approval_records.csv: 审批记录 {len(approval_rows)} 条\n"
                f"  - audit_logs.csv: 审计日志 {len(relevant_audit)} 条\n"
            )
            zf.writestr("README.txt", readme)

    audit.log(
        action="批量合规导出",
        operator="用户",
        target_type="导出",
        target_id=filename,
        detail=(
            f"合规导出打包: 发布{len(records)} + 回滚{len(rollback_records)} + "
            f"审批{len(approval_flows)} + 审计{len(relevant_audit)} 条，文件: {filepath}"
        ),
    )

    return filepath


def query_rollback_records(strategy_id: str = None) -> list:
    records = _load_json(ROLLBACK_RECORDS_FILE)
    if strategy_id:
        records = [r for r in records if r.get("strategy_id") == strategy_id]
    return records
