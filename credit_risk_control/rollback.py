import uuid
import json
import os
from datetime import datetime
from credit_risk_control import (
    RollbackRecord,
    CustomerSegment,
    StrategyStatus,
)
from credit_risk_control.config import (
    ROLLBACK_RECORDS_FILE,
    GRAYSCALE_ORDER,
)
from credit_risk_control import audit_log as audit
from credit_risk_control import notifier


def trigger_rollback(strategy, violations: list) -> RollbackRecord:
    primary_violation = violations[0] if violations else {}
    trigger_metric = primary_violation.get("metric", "未知")
    trigger_value = primary_violation.get("value", 0)
    threshold_config = primary_violation.get("threshold", {})

    if "min" in threshold_config and trigger_metric in ("授信通过率", "欺诈识别率"):
        threshold_value = threshold_config["min"]
    elif "max" in threshold_config:
        threshold_value = threshold_config["max"]
    else:
        threshold_value = 0

    affected_segments = []
    for seg_name in GRAYSCALE_ORDER:
        seg_info = strategy.grayscale_status.get(seg_name, {})
        if seg_info.get("status") == "已推送":
            if seg_name == "优质客群":
                affected_segments.append(CustomerSegment.PREMIUM)
            elif seg_name == "普通客群":
                affected_segments.append(CustomerSegment.NORMAL)
            elif seg_name == "高风险客群":
                affected_segments.append(CustomerSegment.HIGH_RISK)

    compliance_risk = _generate_compliance_risk_desc(strategy, violations)

    previous_version = strategy.previous_stable_version or "v1.0.0-stable"

    record = RollbackRecord(
        rollback_id=f"RLB-{uuid.uuid4().hex[:8].upper()}",
        strategy_id=strategy.strategy_id,
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        reason=f"监控指标 [{trigger_metric}] 超过阈值",
        trigger_metric=trigger_metric,
        trigger_value=trigger_value,
        threshold_value=threshold_value,
        affected_segments=affected_segments,
        compliance_risk_desc=compliance_risk,
        previous_stable_version=previous_version,
        status="已回滚",
        notified_roles=["风控", "授信", "贷后", "合规"],
    )

    strategy.status = StrategyStatus.ROLLED_BACK

    _save_rollback_record(record)
    notifier.notify_rollback(strategy, record)

    audit.log(
        action="风险强制回滚",
        operator="系统",
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=(
            f"策略 {strategy.name} {strategy.version} 因 [{trigger_metric}] 超阈值触发强制回滚。"
            f"当前值 {trigger_value}, 阈值 {threshold_value}。"
            f"已恢复至 {previous_version}。"
            f"受影响客群: {', '.join(s.value for s in affected_segments)}"
        ),
    )

    return record


def generate_rollback_report(record: RollbackRecord) -> str:
    report_lines = [
        "=" * 60,
        "风控策略强制回滚报告",
        "=" * 60,
        f"回滚编号: {record.rollback_id}",
        f"策略名称: {record.strategy_name}",
        f"策略版本: {record.strategy_version}",
        f"回滚时间: {record.created_at}",
        "-" * 40,
        "【回滚原因】",
        f"触发指标: {record.trigger_metric}",
        f"当前值: {record.trigger_value}",
        f"阈值: {record.threshold_value}",
        f"详细说明: {record.reason}",
        "-" * 40,
        "【客群影响范围】",
    ]
    for seg in record.affected_segments:
        report_lines.append(f"  - {seg.value}")
    report_lines.extend([
        "-" * 40,
        "【策略失效原因】",
        f"  {record.reason}",
        "-" * 40,
        "【合规风险说明】",
        f"  {record.compliance_risk_desc}",
        "-" * 40,
        "【恢复方案】",
        f"  已自动恢复至上一稳定版本: {record.previous_stable_version}",
        "  已重启实时风险监控",
        "-" * 40,
        "【通知干系人】",
    ])
    for role in record.notified_roles:
        report_lines.append(f"  - {role}")
    report_lines.append("=" * 60)
    return "\n".join(report_lines)


def restore_previous_strategy(strategy, stable_strategies: list = None):
    if stable_strategies:
        prev = stable_strategies[-1]
        strategy.status = StrategyStatus.ACTIVE
        strategy.version = prev.get("version", strategy.previous_stable_version or "v1.0.0-stable")
        audit.log(
            action="恢复稳定策略",
            operator="系统",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} 已恢复至稳定版本 {strategy.version}，重启实时风险监控",
        )
    else:
        strategy.status = StrategyStatus.ACTIVE
        audit.log(
            action="恢复稳定策略",
            operator="系统",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} 已恢复至上一稳定版本，重启实时风险监控",
        )


def _generate_compliance_risk_desc(strategy, violations: list) -> str:
    risk_descs = []
    for v in violations:
        metric = v.get("metric", "未知指标")
        if metric == "授信通过率":
            risk_descs.append("授信通过率异常可能导致合规风险：审批过松可能违反银保监会贷前审查要求，审批过严可能违反普惠金融政策导向")
        elif metric == "欺诈识别率":
            risk_descs.append("欺诈识别率不足直接违反《反洗钱法》及反欺诈监管要求，可能造成信贷资金流向欺诈团伙")
        elif metric == "放款延迟":
            risk_descs.append("放款延迟超限可能违反贷款合同约定时限，存在合同违约风险及客户投诉风险")
        elif metric == "逾期异常":
            risk_descs.append("逾期异常率飙升可能触发银保监会关注，需评估是否触及不良率监管红线，存在整改风险")

    level_prefix = ""
    if strategy.risk_level.value == "监管风控整改":
        level_prefix = "【高风险-监管整改类策略】该策略属监管要求整改项，指标异常可能直接面临监管处罚。"
    elif strategy.risk_level.value == "紧急欺诈拦截":
        level_prefix = "【高风险-紧急欺诈拦截】该策略用于欺诈拦截，指标异常可能导致大规模欺诈事件。"

    return level_prefix + "；".join(risk_descs) if risk_descs else "暂无明确合规风险"


def _save_rollback_record(record: RollbackRecord):
    os.makedirs(os.path.dirname(ROLLBACK_RECORDS_FILE), exist_ok=True)
    records = []
    if os.path.exists(ROLLBACK_RECORDS_FILE):
        with open(ROLLBACK_RECORDS_FILE, "r", encoding="utf-8") as f:
            try:
                records = json.load(f)
            except json.JSONDecodeError:
                records = []

    records.append({
        "rollback_id": record.rollback_id,
        "strategy_id": record.strategy_id,
        "strategy_name": record.strategy_name,
        "strategy_version": record.strategy_version,
        "reason": record.reason,
        "trigger_metric": record.trigger_metric,
        "trigger_value": record.trigger_value,
        "threshold_value": record.threshold_value,
        "affected_segments": [s.value for s in record.affected_segments],
        "compliance_risk_desc": record.compliance_risk_desc,
        "previous_stable_version": record.previous_stable_version,
        "status": record.status,
        "created_at": record.created_at,
        "notified_roles": record.notified_roles,
    })

    with open(ROLLBACK_RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
