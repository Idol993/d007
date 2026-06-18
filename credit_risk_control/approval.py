import uuid
from credit_risk_control import ApprovalFlow, ApprovalStep, ApprovalRole, RiskLevel
from credit_risk_control.config import APPROVAL_FLOW_RULES


ROLE_ENUM_MAP = {
    "风控": ApprovalRole.RISK_CONTROL,
    "授信": ApprovalRole.CREDIT,
    "法务": ApprovalRole.LEGAL,
    "合规": ApprovalRole.COMPLIANCE,
}


def generate_approval_flow(strategy) -> ApprovalFlow:
    risk_level_name = strategy.risk_level.value
    rules = APPROVAL_FLOW_RULES.get(risk_level_name, APPROVAL_FLOW_RULES["常规策略迭代"])

    steps = []
    for idx, rule in enumerate(rules):
        role_enum = ROLE_ENUM_MAP.get(rule["role"], ApprovalRole.RISK_CONTROL)
        steps.append(
            ApprovalStep(
                step_order=idx + 1,
                role=role_enum,
                approver=rule["approver"],
            )
        )

    flow = ApprovalFlow(
        flow_id=f"APV-{uuid.uuid4().hex[:8].upper()}",
        strategy_id=strategy.strategy_id,
        risk_level=strategy.risk_level,
        steps=steps,
    )

    strategy.approval_flow = flow
    return flow


def simulate_approval(flow: ApprovalFlow, auto_approve: bool = True) -> ApprovalFlow:
    if not auto_approve:
        return flow

    for step in flow.steps:
        step.status = "已通过"
        step.comment = "审批通过-自动化模拟"
        step.approved_at = __import__("datetime").datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    flow.status = "已通过"
    flow.current_step = len(flow.steps)
    return flow
