import random
from credit_risk_control import PrecheckResult
from credit_risk_control.config import PRECHECK_THRESHOLDS


def simulate_metric_value(check_name: str) -> float:
    base = PRECHECK_THRESHOLDS[check_name]["min"]
    if random.random() < 0.85:
        return round(base + random.uniform(0, 0.12), 4)
    else:
        return round(base - random.uniform(0.01, 0.08), 4)


def run_precheck(strategy) -> list:
    results = []
    all_passed = True

    for check_name, config in PRECHECK_THRESHOLDS.items():
        value = simulate_metric_value(check_name)
        threshold = config["min"]
        passed = value >= threshold
        if not passed:
            all_passed = False
        detail = (
            f"当前值: {value:.4f}, 阈值: {threshold:.4f} → {'通过' if passed else '未通过'}"
        )
        results.append(
            PrecheckResult(
                check_name=check_name,
                passed=passed,
                value=value,
                threshold=threshold,
                detail=detail,
            )
        )

    strategy.precheck_results = {
        "all_passed": all_passed,
        "checks": [
            {
                "check_name": r.check_name,
                "passed": r.passed,
                "value": r.value,
                "threshold": r.threshold,
                "detail": r.detail,
            }
            for r in results
        ],
    }

    if all_passed:
        strategy.status = strategy.status.__class__("待审批")
    else:
        strategy.status = strategy.status.__class__("前置检查失败")

    return results
