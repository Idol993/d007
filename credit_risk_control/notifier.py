from credit_risk_control.config import ROLLBACK_NOTIFY_ROLES
from datetime import datetime


def notify(roles: list, subject: str, message: str) -> dict:
    notifications = {}
    for role in roles:
        notifications[role] = {
            "subject": subject,
            "message": message,
            "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "channel": "站内信+邮件",
            "status": "已发送",
        }
    return notifications


def notify_rollback(strategy, rollback_record) -> dict:
    subject = f"【紧急】风控策略强制回滚通知 - {strategy.name} {strategy.version}"
    message = (
        f"策略 {strategy.name}({strategy.version}) 因风险指标超阈值已强制回滚。\n"
        f"回滚原因: {rollback_record.reason}\n"
        f"触发指标: {rollback_record.trigger_metric} 当前值 {rollback_record.trigger_value} 阈值 {rollback_record.threshold_value}\n"
        f"已恢复至稳定版本: {rollback_record.previous_stable_version}\n"
        f"受影响客群: {', '.join(s.value for s in rollback_record.affected_segments)}\n"
        f"合规风险说明: {rollback_record.compliance_risk_desc}\n"
        f"回滚时间: {rollback_record.created_at}"
    )
    return notify(ROLLBACK_NOTIFY_ROLES, subject, message)


def notify_approval(strategy, flow) -> dict:
    current_approvers = []
    if flow.current_step < len(flow.steps):
        step = flow.steps[flow.current_step]
        current_approvers.append(step.role.value)
    subject = f"【待审批】风控策略发布审批 - {strategy.name} {strategy.version}"
    message = (
        f"策略 {strategy.name}({strategy.version}) 已通过前置检查，等待审批。\n"
        f"风险级别: {strategy.risk_level.value}\n"
        f"信贷产品: {strategy.credit_product}\n"
        f"请尽快完成审批。"
    )
    return notify(current_approvers, subject, message)


def notify_grayscale_advance(strategy, segment) -> dict:
    subject = f"【灰度推送】策略灰度阶段推进 - {strategy.name} {strategy.version}"
    message = (
        f"策略 {strategy.name}({strategy.version}) 灰度发布推进至 [{segment.value}] 阶段。\n"
        f"推送比例: {__import__('credit_risk_control.config', fromlist=['GRAYSCALE_RATIOS']).GRAYSCALE_RATIOS.get(segment.value, '100%')}\n"
        f"持续监控运行指标中。"
    )
    return notify(["风控", "授信"], subject, message)
