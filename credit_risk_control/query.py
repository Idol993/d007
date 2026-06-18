import json
import os
import csv
from datetime import datetime
from credit_risk_control.config import (
    PUBLISH_RECORDS_FILE,
    ROLLBACK_RECORDS_FILE,
    EXPORT_DIR,
)
from credit_risk_control import audit_log as audit


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


def batch_export(records: list, export_format: str = "csv") -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if export_format == "json":
        filename = f"publish_export_{timestamp}.json"
        filepath = os.path.join(EXPORT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    else:
        filename = f"publish_export_{timestamp}.csv"
        filepath = os.path.join(EXPORT_DIR, filename)
        if records:
            fieldnames = list(records[0].keys())
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(records)
        else:
            with open(filepath, "w", encoding="utf-8-sig") as f:
                f.write("")

    audit.log(
        action="批量导出",
        operator="用户",
        target_type="发布记录",
        target_id="导出",
        detail=f"导出 {len(records)} 条记录，格式: {export_format}，文件: {filepath}",
    )

    return filepath


def query_rollback_records(strategy_id: str = None) -> list:
    records = _load_json(ROLLBACK_RECORDS_FILE)
    if strategy_id:
        records = [r for r in records if r.get("strategy_id") == strategy_id]
    return records


def _load_json(filepath: str) -> list:
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []
