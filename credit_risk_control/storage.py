import json
import os
from credit_risk_control import RiskStrategy, StrategyStatus, RiskLevel, CustomerSegment
from credit_risk_control.config import (
    STRATEGY_DB_FILE,
    PUBLISH_RECORDS_FILE,
)


def save_strategy(strategy: RiskStrategy):
    os.makedirs(os.path.dirname(STRATEGY_DB_FILE), exist_ok=True)
    strategies = load_all_strategies()
    found = False
    for i, s in enumerate(strategies):
        if s["strategy_id"] == strategy.strategy_id:
            strategies[i] = _strategy_to_dict(strategy)
            found = True
            break
    if not found:
        strategies.append(_strategy_to_dict(strategy))
    with open(STRATEGY_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(strategies, f, ensure_ascii=False, indent=2)


def load_all_strategies() -> list:
    if not os.path.exists(STRATEGY_DB_FILE):
        return []
    with open(STRATEGY_DB_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def load_strategy(strategy_id: str) -> dict:
    strategies = load_all_strategies()
    for s in strategies:
        if s["strategy_id"] == strategy_id:
            return s
    return {}


def save_publish_record(record: dict):
    os.makedirs(os.path.dirname(PUBLISH_RECORDS_FILE), exist_ok=True)
    records = []
    if os.path.exists(PUBLISH_RECORDS_FILE):
        with open(PUBLISH_RECORDS_FILE, "r", encoding="utf-8") as f:
            try:
                records = json.load(f)
            except json.JSONDecodeError:
                records = []
    records.append(record)
    with open(PUBLISH_RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _strategy_to_dict(strategy: RiskStrategy) -> dict:
    return {
        "strategy_id": strategy.strategy_id,
        "name": strategy.name,
        "version": strategy.version,
        "risk_level": strategy.risk_level.value,
        "description": strategy.description,
        "credit_product": strategy.credit_product,
        "status": strategy.status.value,
        "created_at": strategy.created_at,
        "updated_at": strategy.updated_at,
        "precheck_results": strategy.precheck_results,
        "grayscale_status": strategy.grayscale_status,
        "monitoring_data": strategy.monitoring_data,
        "previous_stable_version": strategy.previous_stable_version,
    }
