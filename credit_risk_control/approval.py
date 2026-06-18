import uuid
import json
import os
from datetime import datetime
from credit_risk_control import ApprovalFlow, ApprovalStep, ApprovalRole, RiskLevel
from credit_risk_control.config import APPROVAL_FLOW_RULES, APPROVAL_FLOWS_FILE
from credit_risk_control import audit_log as audit


ROLE_ENUM_MAP = {
    "风控": ApprovalRole.RISK_CONTROL,
    "授信": ApprovalRole.CREDIT,
    "法务": ApprovalRole.LEGAL,
    "合规": ApprovalRole.COMPLIANCE,
}


def _load_flows() -> list:
    if not os.path.exists(APPROVAL_FLOWS_FILE):
        return []
    with open(APPROVAL_FLOWS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save_flows(flows: list):
    os.makedirs(os.path.dirname(APPROVAL_FLOWS_FILE), exist_ok=True)
    with open(APPROVAL_FLOWS_FILE, "w", encoding="utf-8") as f:
        json.dump(flows, f, ensure_ascii=False, indent=2)


def _flow_to_dict(flow: ApprovalFlow) -> dict:
    return {
        "flow_id": flow.flow_id,
        "strategy_id": flow.strategy_id,
        "risk_level": flow.risk_level.value,
        "status": flow.status,
        "current_step": flow.current_step,
        "created_at": flow.created_at,
        "steps": [
            {
                "step_order": s.step_order,
                "role": s.role.value,
                "approver": s.approver,
                "status": s.status,
                "comment": s.comment,
                "approved_at": s.approved_at,
            }
            for s in flow.steps
        ],
    }


def _dict_to_flow(d: dict) -> ApprovalFlow:
    risk_map = {
        "常规策略迭代": RiskLevel.ROUTINE,
        "紧急欺诈拦截": RiskLevel.EMERGENCY_FRAUD,
        "监管风控整改": RiskLevel.REGULATORY,
    }
    steps = [
        ApprovalStep(
            step_order=s["step_order"],
            role=ROLE_ENUM_MAP.get(s["role"], ApprovalRole.RISK_CONTROL),
            approver=s["approver"],
            status=s.get("status", "待审批"),
            comment=s.get("comment", ""),
            approved_at=s.get("approved_at"),
        )
        for s in d.get("steps", [])
    ]
    return ApprovalFlow(
        flow_id=d["flow_id"],
        strategy_id=d["strategy_id"],
        risk_level=risk_map.get(d.get("risk_level"), RiskLevel.ROUTINE),
        steps=steps,
        current_step=d.get("current_step", 0),
        status=d.get("status", "待审批"),
        created_at=d.get("created_at"),
    )


def _persist_flow(flow: ApprovalFlow):
    flows = _load_flows()
    found = False
    for i, f in enumerate(flows):
        if f["flow_id"] == flow.flow_id:
            flows[i] = _flow_to_dict(flow)
            found = True
            break
    if not found:
        flows.append(_flow_to_dict(flow))
    _save_flows(flows)


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
    _persist_flow(flow)
    audit.log(
        action="生成审批流程",
        operator="系统",
        target_type="审批流程",
        target_id=flow.flow_id,
        detail=(
            f"策略 {strategy.name} {strategy.version} 生成审批流程 {flow.flow_id}，"
            f"风险级别 {strategy.risk_level.value}，共 {len(steps)} 个审批步骤"
        ),
    )
    return flow


def simulate_approval(flow: ApprovalFlow, auto_approve: bool = True) -> ApprovalFlow:
    if not auto_approve:
        return flow

    for step in flow.steps:
        step.status = "已通过"
        step.comment = "审批通过-自动化模拟"
        step.approved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    flow.status = "已通过"
    flow.current_step = len(flow.steps)
    _persist_flow(flow)
    audit.log(
        action="自动审批通过",
        operator="系统",
        target_type="审批流程",
        target_id=flow.flow_id,
        detail=f"审批流程 {flow.flow_id} 自动完成全流程审批",
    )
    return flow


def step_approve(flow: ApprovalFlow, comment: str = "审批通过", approved: bool = True) -> ApprovalFlow:
    if flow.status in ("已通过", "已驳回"):
        return flow
    if flow.current_step >= len(flow.steps):
        return flow

    step = flow.steps[flow.current_step]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if approved:
        step.status = "已通过"
        step.comment = comment
        step.approved_at = now
        flow.current_step += 1
        if flow.current_step >= len(flow.steps):
            flow.status = "已通过"
        audit.log(
            action="审批通过",
            operator=step.approver,
            target_type="审批流程",
            target_id=flow.flow_id,
            detail=f"{step.approver}[{step.role.value}] 审批通过审批流程 {flow.flow_id} 的第 {step.step_order} 步，备注: {comment}",
        )
    else:
        step.status = "已驳回"
        step.comment = comment
        step.approved_at = now
        flow.status = "已驳回"
        audit.log(
            action="审批驳回",
            operator=step.approver,
            target_type="审批流程",
            target_id=flow.flow_id,
            detail=f"{step.approver}[{step.role.value}] 驳回审批流程 {flow.flow_id} 的第 {step.step_order} 步，原因: {comment}",
        )

    _persist_flow(flow)
    return flow


def get_flow_by_id(flow_id: str):
    flows = _load_flows()
    for f in flows:
        if f["flow_id"] == flow_id:
            return _dict_to_flow(f)
    return None


def get_flow_by_strategy(strategy_id: str):
    flows = _load_flows()
    result = []
    for f in flows:
        if f["strategy_id"] == strategy_id:
            result.append(_dict_to_flow(f))
    return result


def query_approval_ledger(
    approver: str = None,
    role: str = None,
    risk_level: str = None,
    status: str = None,
) -> list:
    flows = _load_flows()
    result = []

    for f in flows:
        if risk_level and f.get("risk_level") != risk_level:
            continue
        if status and f.get("status") != status:
            continue

        matched_steps = []
        for step in f.get("steps", []):
            if approver and approver not in step.get("approver", ""):
                continue
            if role and step.get("role") != role:
                continue
            matched_steps.append(step)

        if not (approver or role) or matched_steps:
            entry = {
                "flow_id": f["flow_id"],
                "strategy_id": f["strategy_id"],
                "risk_level": f["risk_level"],
                "status": f["status"],
                "current_step": f["current_step"],
                "created_at": f["created_at"],
                "steps": matched_steps if (approver or role) else f.get("steps", []),
            }
            result.append(entry)

    return result


def list_pending_tasks(approver: str = None, role: str = None) -> list:
    return query_approval_ledger(approver=approver, role=role, status="待审批")


def list_completed_approvals(approver: str = None, role: str = None) -> list:
    return query_approval_ledger(approver=approver, role=role, status="已通过")


def list_rejected_approvals(approver: str = None, role: str = None) -> list:
    return query_approval_ledger(approver=approver, role=role, status="已驳回")
