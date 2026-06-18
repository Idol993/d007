import time
from credit_risk_control import CustomerSegment, StrategyStatus
from credit_risk_control.config import GRAYSCALE_ORDER, GRAYSCALE_RATIOS
from credit_risk_control import audit_log as audit
from credit_risk_control import notifier


SEGMENT_ENUM_MAP = {
    "优质客群": CustomerSegment.PREMIUM,
    "普通客群": CustomerSegment.NORMAL,
    "高风险客群": CustomerSegment.HIGH_RISK,
}


def init_grayscale(strategy) -> dict:
    grayscale_status = {}
    for segment_name in GRAYSCALE_ORDER:
        grayscale_status[segment_name] = {
            "segment": segment_name,
            "ratio": GRAYSCALE_RATIOS[segment_name],
            "status": "待推送",
            "started_at": None,
            "completed_at": None,
        }
    strategy.grayscale_status = grayscale_status
    strategy.status = StrategyStatus.GRAYSCALE_DEPLOYING
    return grayscale_status


def advance_grayscale(strategy, monitor_check_func=None) -> dict:
    results = {"advanced": [], "completed": False, "rolled_back": False}

    for segment_name in GRAYSCALE_ORDER:
        seg_info = strategy.grayscale_status.get(segment_name)
        if seg_info is None:
            continue
        if seg_info["status"] == "已推送":
            continue
        if seg_info["status"] in ("待推送", "推送中"):
            if monitor_check_func and not monitor_check_func(strategy):
                results["rolled_back"] = True
                return results

            seg_info["status"] = "已推送"
            seg_info["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            seg_info["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

            segment_enum = SEGMENT_ENUM_MAP.get(segment_name, CustomerSegment.NORMAL)
            notifier.notify_grayscale_advance(strategy, segment_enum)

            audit.log(
                action="灰度推送",
                operator="系统",
                target_type="策略",
                target_id=strategy.strategy_id,
                detail=f"策略 {strategy.name} {strategy.version} 灰度推送至 [{segment_name}] 比例 {GRAYSCALE_RATIOS[segment_name]*100:.0f}%",
            )
            results["advanced"].append(segment_name)
            break

    all_done = all(
        strategy.grayscale_status.get(s, {}).get("status") == "已推送"
        for s in GRAYSCALE_ORDER
    )
    if all_done:
        strategy.status = StrategyStatus.FULL_DEPLOYED
        results["completed"] = True
        audit.log(
            action="全量发布",
            operator="系统",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} {strategy.version} 已完成全量灰度发布，正式生效",
        )

    return results
