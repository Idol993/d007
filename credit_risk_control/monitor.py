import random
import json
import os
import time
from datetime import datetime
from credit_risk_control import MonitorSnapshot
from credit_risk_control.config import (
    MONITOR_THRESHOLDS,
    MONITOR_INTERVAL_SECONDS,
    MONITOR_SNAPSHOTS_FILE,
)


def simulate_monitor_snapshot(strategy_id: str = "") -> MonitorSnapshot:
    return MonitorSnapshot(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        credit_approval_rate=round(random.uniform(0.55, 0.98), 4),
        fraud_detection_rate=round(random.uniform(0.75, 0.99), 4),
        loan_delay_seconds=round(random.uniform(50, 400), 1),
        overdue_anomaly_rate=round(random.uniform(0.01, 0.08), 4),
        strategy_id=strategy_id,
    )


def check_thresholds(snapshot: MonitorSnapshot) -> list:
    violations = []
    metrics = {
        "授信通过率": snapshot.credit_approval_rate,
        "欺诈识别率": snapshot.fraud_detection_rate,
        "放款延迟": snapshot.loan_delay_seconds,
        "逾期异常": snapshot.overdue_anomaly_rate,
    }

    for metric_name, value in metrics.items():
        config = MONITOR_THRESHOLDS.get(metric_name)
        if not config:
            continue
        direction = config.get("direction", "min")
        violated = False

        if direction == "min":
            if value < config["min"]:
                violated = True
        elif direction == "max":
            if value > config["max"]:
                violated = True
        elif direction == "range":
            if "min" in config and value < config["min"]:
                violated = True
            if "max" in config and value > config["max"]:
                violated = True

        if violated:
            violations.append({
                "metric": metric_name,
                "value": value,
                "threshold": config,
                "direction": direction,
            })

    return violations


def save_snapshot(snapshot: MonitorSnapshot):
    os.makedirs(os.path.dirname(MONITOR_SNAPSHOTS_FILE), exist_ok=True)
    snapshots = []
    if os.path.exists(MONITOR_SNAPSHOTS_FILE):
        with open(MONITOR_SNAPSHOTS_FILE, "r", encoding="utf-8") as f:
            try:
                snapshots = json.load(f)
            except json.JSONDecodeError:
                snapshots = []

    snapshots.append({
        "timestamp": snapshot.timestamp,
        "credit_approval_rate": snapshot.credit_approval_rate,
        "fraud_detection_rate": snapshot.fraud_detection_rate,
        "loan_delay_seconds": snapshot.loan_delay_seconds,
        "overdue_anomaly_rate": snapshot.overdue_anomaly_rate,
        "strategy_id": snapshot.strategy_id,
    })

    with open(MONITOR_SNAPSHOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshots, f, ensure_ascii=False, indent=2)


def monitor_cycle(strategy, on_violation_callback=None) -> dict:
    snapshot = simulate_monitor_snapshot(strategy.strategy_id)
    save_snapshot(snapshot)
    violations = check_thresholds(snapshot)

    result = {
        "snapshot": {
            "timestamp": snapshot.timestamp,
            "credit_approval_rate": snapshot.credit_approval_rate,
            "fraud_detection_rate": snapshot.fraud_detection_rate,
            "loan_delay_seconds": snapshot.loan_delay_seconds,
            "overdue_anomaly_rate": snapshot.overdue_anomaly_rate,
        },
        "violations": violations,
        "need_rollback": len(violations) > 0,
    }

    if violations and on_violation_callback:
        on_violation_callback(strategy, violations)

    return result


def is_strategy_healthy(strategy) -> bool:
    snapshot = simulate_monitor_snapshot(strategy.strategy_id)
    violations = check_thresholds(snapshot)
    return len(violations) == 0
