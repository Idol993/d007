import uuid
import json
import os
import random
from datetime import datetime
from credit_risk_control import DrillRecord
from credit_risk_control.config import DRILL_RECORDS_FILE
from credit_risk_control import audit_log as audit


def create_drill(strategy, operator: str = "管理员") -> DrillRecord:
    plan = _generate_drill_plan(strategy)
    fraud_result = _simulate_fraud_check(strategy)
    disposal_result = _simulate_risk_disposal(strategy)

    record = DrillRecord(
        drill_id=f"DRL-{uuid.uuid4().hex[:8].upper()}",
        strategy_id=strategy.strategy_id,
        plan=plan,
        fraud_simulation_result=fraud_result,
        risk_disposal_result=disposal_result,
        status="已完成",
    )

    _save_drill_record(record)

    audit.log(
        action="回滚演练",
        operator=operator,
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=(
            f"策略 {strategy.name} {strategy.version} 完成回滚演练。"
            f"演练计划: {plan[:50]}... "
            f"欺诈模拟结果: {fraud_result[:50]}... "
            f"风险处置结果: {disposal_result[:50]}..."
        ),
    )

    return record


def _generate_drill_plan(strategy) -> str:
    level_plans = {
        "常规策略迭代": (
            f"1. 验证策略 {strategy.name} {strategy.version} 回滚至上一版本的完整流程；"
            f"2. 模拟授信通过率下降至55%的场景，测试自动回滚触发；"
            f"3. 验证回滚后客群策略恢复正确性；"
            f"4. 检查审计日志记录完整性。"
        ),
        "紧急欺诈拦截": (
            f"1. 验证紧急欺诈拦截策略回滚的快速响应机制（要求5分钟内完成回滚）；"
            f"2. 模拟欺诈识别率下降至70%的紧急场景；"
            f"3. 验证全客群回滚及通知机制；"
            f"4. 验证恢复稳定策略后欺诈拦截功能正常；"
            f"5. 检查监管报备流程完整性。"
        ),
        "监管风控整改": (
            f"1. 验证监管整改策略回滚的合规审批流程；"
            f"2. 模拟逾期异常率飙升至8%的监管预警场景；"
            f"3. 验证回滚操作符合银保监会整改要求；"
            f"4. 验证回滚后合规指标恢复达标；"
            f"5. 生成监管报备材料完整性校验；"
            f"6. 检查审计日志满足监管检查要求。"
        ),
    }
    return level_plans.get(strategy.risk_level.value, level_plans["常规策略迭代"])


def _simulate_fraud_check(strategy) -> str:
    patterns = [
        "身份冒用欺诈", "团伙欺诈", "中介代办欺诈", "资料伪造欺诈", "设备欺诈"
    ]
    pattern = random.choice(patterns)
    detection_rate = round(random.uniform(0.85, 0.99), 4)
    false_positive = round(random.uniform(0.01, 0.05), 4)

    result = (
        f"欺诈模拟校验完成。模拟场景: [{pattern}]。"
        f"策略检测率: {detection_rate*100:.2f}%，误报率: {false_positive*100:.2f}%。"
    )

    if detection_rate >= 0.90:
        result += "欺诈识别能力达标，回滚后策略可保持有效拦截。"
    else:
        result += "欺诈识别能力不足，建议回滚后补充规则后重新发布。"

    return result


def _simulate_risk_disposal(strategy) -> str:
    actions = [
        "自动暂停高风险客群授信",
        "触发反欺诈规则集切换",
        "通知贷后团队进行存量客户排查",
        "启动应急预案降级审批流程",
    ]
    selected = random.sample(actions, min(2, len(actions)))
    elapsed = round(random.uniform(30, 180), 1)

    result = (
        f"风险处置演练完成。执行操作: {'; '.join(selected)}。"
        f"处置响应时间: {elapsed}秒。"
    )

    if elapsed <= 120:
        result += "响应时间达标（≤120秒），风险处置流程验证通过。"
    else:
        result += "响应时间超限，建议优化自动化回滚流程。"

    return result


def _save_drill_record(record: DrillRecord):
    os.makedirs(os.path.dirname(DRILL_RECORDS_FILE), exist_ok=True)
    records = []
    if os.path.exists(DRILL_RECORDS_FILE):
        with open(DRILL_RECORDS_FILE, "r", encoding="utf-8") as f:
            try:
                records = json.load(f)
            except json.JSONDecodeError:
                records = []

    records.append({
        "drill_id": record.drill_id,
        "strategy_id": record.strategy_id,
        "plan": record.plan,
        "fraud_simulation_result": record.fraud_simulation_result,
        "risk_disposal_result": record.risk_disposal_result,
        "status": record.status,
        "created_at": record.created_at,
    })

    with open(DRILL_RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
