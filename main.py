#!/usr/bin/env python3
import sys
import os
import uuid
import argparse
import time
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from credit_risk_control import (
    RiskStrategy,
    RiskLevel,
    CustomerSegment,
    StrategyStatus,
    PublishRecord,
)
from credit_risk_control import config
from credit_risk_control import precheck
from credit_risk_control import approval
from credit_risk_control import grayscale
from credit_risk_control import monitor
from credit_risk_control import rollback as rollback_mod
from credit_risk_control import drill as drill_mod
from credit_risk_control import report as report_mod
from credit_risk_control import query as query_mod
from credit_risk_control import audit_log as audit
from credit_risk_control import storage
from credit_risk_control import notifier


BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║           银行信贷风控系统 - 策略发布与风险回滚管理           ║
║     Credit Risk Control: Strategy Publish & Rollback         ║
╚══════════════════════════════════════════════════════════════╝
"""

MONITOR_THREAD = None
MONITOR_STOP_EVENT = threading.Event()


def create_strategy(args):
    name = args.name
    version = args.version or f"v{datetime.now().strftime('%Y%m%d')}.1"
    risk_level_map = {
        "常规": RiskLevel.ROUTINE,
        "紧急": RiskLevel.EMERGENCY_FRAUD,
        "监管": RiskLevel.REGULATORY,
    }
    risk_level = risk_level_map.get(args.risk_level, RiskLevel.ROUTINE)
    product = args.product or "个人消费贷"
    desc = args.description or f"{name} 风控策略"

    strategy = RiskStrategy(
        strategy_id=f"STR-{uuid.uuid4().hex[:8].upper()}",
        name=name,
        version=version,
        risk_level=risk_level,
        description=desc,
        credit_product=product,
        status=StrategyStatus.DRAFT,
        previous_stable_version="v1.0.0-stable",
    )

    storage.save_strategy(strategy)

    audit.log(
        action="创建策略",
        operator=args.operator or "管理员",
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=f"创建风控策略 {name} {version}，风险级别: {risk_level.value}，产品: {product}",
    )

    print(f"\n✅ 策略创建成功")
    print(f"   策略ID: {strategy.strategy_id}")
    print(f"   名称: {strategy.name}")
    print(f"   版本: {strategy.version}")
    print(f"   风险级别: {strategy.risk_level.value}")
    print(f"   信贷产品: {strategy.credit_product}")
    print(f"   状态: {strategy.status.value}")
    return strategy


def publish_strategy(args):
    strategy_id = args.strategy_id
    strategy_dict = storage.load_strategy(strategy_id)
    if not strategy_dict:
        print(f"❌ 未找到策略: {strategy_id}")
        return None

    strategy = _dict_to_strategy(strategy_dict)

    print(f"\n{'='*60}")
    print(f"策略发布流程启动: {strategy.name} {strategy.version}")
    print(f"{'='*60}")

    print(f"\n📋 阶段1: 前置条件检查...")
    strategy.status = StrategyStatus.PENDING_PRECHECK
    results = precheck.run_precheck(strategy)

    all_passed = True
    for r in results:
        status_icon = "✅" if r.passed else "❌"
        print(f"   {status_icon} {r.check_name}: {r.detail}")
        if not r.passed:
            all_passed = False

    if not all_passed:
        strategy.status = StrategyStatus.PRECHECK_FAILED
        storage.save_strategy(strategy)
        audit.log(
            action="前置检查失败",
            operator="系统",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} {strategy.version} 前置检查未通过",
        )
        print(f"\n❌ 前置检查未通过，策略发布终止")
        return None

    print(f"\n📋 阶段2: 生成信贷合规审批流程...")
    flow = approval.generate_approval_flow(strategy)
    print(f"   审批流程ID: {flow.flow_id}")
    print(f"   风险级别: {strategy.risk_level.value}")
    for step in flow.steps:
        print(f"   步骤{step.step_order}: [{step.role.value}] {step.approver} - {step.status}")

    print(f"\n📋 阶段3: 模拟审批流程...")
    approval.simulate_approval(flow, auto_approve=True)
    print(f"   审批结果: {flow.status}")
    for step in flow.steps:
        print(f"   步骤{step.step_order}: [{step.role.value}] {step.approver} - {step.status} ({step.approved_at})")

    audit.log(
        action="审批通过",
        operator="系统",
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=f"策略 {strategy.name} {strategy.version} 审批流程 {flow.flow_id} 已通过",
    )

    print(f"\n📋 阶段4: 客群灰度策略推送...")
    grayscale.init_grayscale(strategy)

    for segment_name in config.GRAYSCALE_ORDER:
        result = grayscale.advance_grayscale(strategy, monitor_check_func=monitor.is_strategy_healthy)
        if result.get("rolled_back"):
            print(f"\n⚠️  灰度推送过程中监控指标异常，触发自动回滚！")
            _handle_rollback(strategy, [])
            return strategy
        advanced = result.get("advanced", [])
        for seg in advanced:
            seg_info = strategy.grayscale_status.get(seg, {})
            print(f"   ✅ 推送至 [{seg}] 比例: {seg_info.get('ratio', 0)*100:.0f}%")
        if result.get("completed"):
            print(f"   ✅ 全量灰度发布完成！")

    storage.save_strategy(strategy)

    for segment_name in config.GRAYSCALE_ORDER:
        seg_enum = _segment_name_to_enum(segment_name)
        record = {
            "record_id": f"REC-{uuid.uuid4().hex[:8].upper()}",
            "strategy_id": strategy.strategy_id,
            "strategy_name": strategy.name,
            "version": strategy.version,
            "risk_level": strategy.risk_level.value,
            "credit_product": strategy.credit_product,
            "customer_segment": segment_name,
            "status": strategy.status.value,
            "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator": args.operator or "管理员",
            "rollback_count": 0,
        }
        storage.save_publish_record(record)

    audit.log(
        action="策略发布完成",
        operator=args.operator or "管理员",
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=f"策略 {strategy.name} {strategy.version} 发布完成，状态: {strategy.status.value}",
    )

    print(f"\n{'='*60}")
    print(f"✅ 策略发布完成！")
    print(f"   策略: {strategy.name} {strategy.version}")
    print(f"   状态: {strategy.status.value}")
    print(f"{'='*60}")

    if args.monitor:
        start_monitoring(strategy)

    return strategy


def _handle_rollback(strategy, violations):
    record = rollback_mod.trigger_rollback(strategy, violations)
    report_text = rollback_mod.generate_rollback_report(record)
    print(f"\n{report_text}")

    rollback_mod.restore_previous_strategy(strategy)
    storage.save_strategy(strategy)

    print(f"\n✅ 已自动恢复至上一稳定风控策略，重启实时风险监控")


def start_monitoring(strategy):
    global MONITOR_THREAD, MONITOR_STOP_EVENT

    MONITOR_STOP_EVENT.clear()
    print(f"\n🔍 启动实时风险监控 (每{config.MONITOR_INTERVAL_SECONDS}秒检查一次)")
    print(f"   策略: {strategy.name} {strategy.version}")
    print(f"   按 Ctrl+C 停止监控\n")

    cycle_count = 0
    try:
        while not MONITOR_STOP_EVENT.is_set():
            cycle_count += 1
            result = monitor.monitor_cycle(
                strategy,
                on_violation_callback=None,
            )
            snap = result["snapshot"]
            violations = result["violations"]

            status_icons = "✅" if not violations else "⚠️ "
            print(
                f"  [{snap['timestamp']}] {status_icons} "
                f"授信通过率: {snap['credit_approval_rate']*100:.2f}% | "
                f"欺诈识别率: {snap['fraud_detection_rate']*100:.2f}% | "
                f"放款延迟: {snap['loan_delay_seconds']:.1f}s | "
                f"逾期异常: {snap['overdue_anomaly_rate']*100:.2f}%"
            )

            if violations:
                print(f"\n  ⚠️  检测到风险指标超阈值！")
                for v in violations:
                    print(f"     - {v['metric']}: 当前值 {v['value']}, 阈值 {v['threshold']}")

                audit.log(
                    action="监控告警",
                    operator="系统",
                    target_type="策略",
                    target_id=strategy.strategy_id,
                    detail=f"监控指标超阈值: {', '.join(v['metric'] for v in violations)}",
                )

                if not args_noninteractive:
                    print(f"\n  是否触发强制回滚？(y/n): ", end="")
                    choice = input().strip().lower()
                else:
                    choice = "y"

                if choice == "y":
                    _handle_rollback(strategy, violations)
                    break

            MONITOR_STOP_EVENT.wait(config.MONITOR_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print(f"\n\n⏹  监控已停止 (共运行 {cycle_count} 个周期)")


def run_drill(args):
    strategy_id = args.strategy_id
    strategy_dict = storage.load_strategy(strategy_id)
    if not strategy_dict:
        print(f"❌ 未找到策略: {strategy_id}")
        return None

    strategy = _dict_to_strategy(strategy_dict)

    print(f"\n{'='*60}")
    print(f"风控策略回滚演练")
    print(f"{'='*60}")
    print(f"策略: {strategy.name} {strategy.version}")
    print(f"风险级别: {strategy.risk_level.value}")

    record = drill_mod.create_drill(strategy, operator=args.operator or "管理员")

    print(f"\n📋 演练计划:")
    for line in record.plan.split("；"):
        print(f"   {line.strip()}")
    print(f"\n🔍 欺诈模拟校验结果:")
    print(f"   {record.fraud_simulation_result}")
    print(f"\n🛡️ 风险处置结果:")
    print(f"   {record.risk_disposal_result}")
    print(f"\n✅ 演练完成！演练ID: {record.drill_id}")

    return record


def generate_report(args):
    week_offset = args.week_offset or 0
    print(f"\n{'='*60}")
    print(f"生成信贷风控周报")
    print(f"{'='*60}")

    stats = report_mod.generate_weekly_report(week_offset)

    print(f"\n📊 周报统计 (周期: {stats['period']})")
    print(f"   策略发布总数: {stats['total_publishes']}")
    print(f"   发布成功数: {stats['success_publishes']}")
    print(f"   发布成功率: {stats['publish_success_rate']*100:.2f}%")
    print(f"   回滚次数: {stats['rollback_count']}")
    print(f"   平均欺诈拦截率: {stats['avg_fraud_detection_rate']*100:.2f}%")
    print(f"\n📈 每日风险趋势:")
    print(f"   {'日期':<14} {'授信通过率':<14} {'欺诈识别率':<14} {'逾期异常率':<14}")
    for day in stats["trend_data"]:
        print(
            f"   {day['date']:<14} "
            f"{day['credit_approval_rate']*100:>8.2f}%      "
            f"{day['fraud_detection_rate']*100:>8.2f}%      "
            f"{day['overdue_anomaly_rate']*100:>8.2f}%"
        )
    print(f"\n📄 报告文件:")
    print(f"   PDF: {stats['pdf_path']}")
    print(f"   Excel: {stats['excel_path']}")


def query_records(args):
    records = query_mod.query_publish_records(
        publish_time_start=args.time_start,
        publish_time_end=args.time_end,
        credit_product=args.product,
        customer_segment=args.segment,
        version=args.version,
    )

    print(f"\n{'='*60}")
    print(f"历史发布记录查询 (共 {len(records)} 条)")
    print(f"{'='*60}")

    if not records:
        print("   未查询到匹配的记录")
        return

    for r in records:
        print(f"\n   记录ID: {r.get('record_id')}")
        print(f"   策略: {r.get('strategy_name')} {r.get('version')}")
        print(f"   风险级别: {r.get('risk_level')}")
        print(f"   信贷产品: {r.get('credit_product')}")
        print(f"   客群: {r.get('customer_segment')}")
        print(f"   状态: {r.get('status')}")
        print(f"   发布时间: {r.get('publish_time')}")

    if args.export:
        fmt = args.export_format or "csv"
        filepath = query_mod.batch_export(records, export_format=fmt)
        print(f"\n📤 已导出至: {filepath}")


def show_audit_logs(args):
    logs = audit.query_logs(
        date=args.date,
        action=args.action,
        target_id=args.target_id,
    )

    print(f"\n{'='*60}")
    print(f"风险审计日志 (共 {len(logs)} 条)")
    print(f"{'='*60}")

    for entry in logs[-50:]:
        print(f"   [{entry.get('timestamp')}] {entry.get('action')} | {entry.get('operator')} | {entry.get('detail')[:80]}")

    if len(logs) > 50:
        print(f"\n   ... 仅显示最近 50 条，共 {len(logs)} 条记录")


def list_strategies(args):
    strategies = storage.load_all_strategies()

    print(f"\n{'='*60}")
    print(f"风控策略列表 (共 {len(strategies)} 条)")
    print(f"{'='*60}")

    if not strategies:
        print("   暂无策略")
        return

    for s in strategies:
        print(f"\n   ID: {s.get('strategy_id')}")
        print(f"   名称: {s.get('name')} {s.get('version')}")
        print(f"   风险级别: {s.get('risk_level')}")
        print(f"   产品: {s.get('credit_product')}")
        print(f"   状态: {s.get('status')}")
        print(f"   创建时间: {s.get('created_at')}")


def _dict_to_strategy(d: dict) -> RiskStrategy:
    risk_level_map = {
        "常规策略迭代": RiskLevel.ROUTINE,
        "紧急欺诈拦截": RiskLevel.EMERGENCY_FRAUD,
        "监管风控整改": RiskLevel.REGULATORY,
    }
    status_map = {s.value: s for s in StrategyStatus}

    return RiskStrategy(
        strategy_id=d.get("strategy_id", ""),
        name=d.get("name", ""),
        version=d.get("version", ""),
        risk_level=risk_level_map.get(d.get("risk_level", ""), RiskLevel.ROUTINE),
        description=d.get("description", ""),
        credit_product=d.get("credit_product", ""),
        status=status_map.get(d.get("status", ""), StrategyStatus.DRAFT),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        precheck_results=d.get("precheck_results", {}),
        grayscale_status=d.get("grayscale_status", {}),
        monitoring_data=d.get("monitoring_data", {}),
        previous_stable_version=d.get("previous_stable_version"),
    )


def _segment_name_to_enum(name: str) -> CustomerSegment:
    mapping = {
        "优质客群": CustomerSegment.PREMIUM,
        "普通客群": CustomerSegment.NORMAL,
        "高风险客群": CustomerSegment.HIGH_RISK,
    }
    return mapping.get(name, CustomerSegment.NORMAL)


args_noninteractive = False


def main():
    global args_noninteractive

    parser = argparse.ArgumentParser(
        description="银行信贷风控系统 - 策略发布与风险回滚自动化管理"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    create_parser = subparsers.add_parser("create", help="创建风控策略")
    create_parser.add_argument("--name", required=True, help="策略名称")
    create_parser.add_argument("--version", help="策略版本")
    create_parser.add_argument(
        "--risk-level", choices=["常规", "紧急", "监管"], default="常规", help="风险级别"
    )
    create_parser.add_argument("--product", help="信贷产品")
    create_parser.add_argument("--description", help="策略描述")
    create_parser.add_argument("--operator", help="操作人")

    publish_parser = subparsers.add_parser("publish", help="发布风控策略（含全流程）")
    publish_parser.add_argument("--strategy-id", required=True, help="策略ID")
    publish_parser.add_argument("--monitor", action="store_true", help="发布后启动实时监控")
    publish_parser.add_argument("--operator", help="操作人")

    monitor_parser = subparsers.add_parser("monitor", help="启动实时风险监控")
    monitor_parser.add_argument("--strategy-id", required=True, help="策略ID")

    drill_parser = subparsers.add_parser("drill", help="风控策略回滚演练")
    drill_parser.add_argument("--strategy-id", required=True, help="策略ID")
    drill_parser.add_argument("--operator", help="操作人")

    report_parser = subparsers.add_parser("report", help="生成周报")
    report_parser.add_argument("--week-offset", type=int, default=0, help="周偏移量(0=本周)")

    query_parser = subparsers.add_parser("query", help="查询历史发布记录")
    query_parser.add_argument("--time-start", help="开始时间(YYYY-MM-DD)")
    query_parser.add_argument("--time-end", help="结束时间(YYYY-MM-DD)")
    query_parser.add_argument("--product", help="信贷产品")
    query_parser.add_argument("--segment", help="客群类型")
    query_parser.add_argument("--version", help="版本号")
    query_parser.add_argument("--export", action="store_true", help="导出记录")
    query_parser.add_argument("--export-format", choices=["csv", "json"], default="csv", help="导出格式")

    audit_parser = subparsers.add_parser("audit", help="查看风险审计日志")
    audit_parser.add_argument("--date", help="日期(YYYY-MM-DD)")
    audit_parser.add_argument("--action", help="操作类型")
    audit_parser.add_argument("--target-id", help="目标ID")

    subparsers.add_parser("list", help="列出所有策略")

    demo_parser = subparsers.add_parser("demo", help="运行完整演示流程")
    demo_parser.add_argument("--non-interactive", action="store_true", help="非交互模式")

    args = parser.parse_args()

    if args.command == "demo":
        args_noninteractive = getattr(args, "non_interactive", True)
        run_demo()
    elif args.command == "create":
        create_strategy(args)
    elif args.command == "publish":
        publish_strategy(args)
    elif args.command == "monitor":
        strategy_dict = storage.load_strategy(args.strategy_id)
        if strategy_dict:
            strategy = _dict_to_strategy(strategy_dict)
            args_noninteractive = False
            start_monitoring(strategy)
        else:
            print(f"❌ 未找到策略: {args.strategy_id}")
    elif args.command == "drill":
        run_drill(args)
    elif args.command == "report":
        generate_report(args)
    elif args.command == "query":
        query_records(args)
    elif args.command == "audit":
        show_audit_logs(args)
    elif args.command == "list":
        list_strategies(args)
    else:
        parser.print_help()


def run_demo():
    global args_noninteractive
    args_noninteractive = True

    print(BANNER)
    print("🚀 启动完整演示流程\n")

    print("=" * 60)
    print("场景1: 常规策略迭代 - 完整发布流程")
    print("=" * 60)

    strategy = RiskStrategy(
        strategy_id=f"STR-{uuid.uuid4().hex[:8].upper()}",
        name="个人消费贷风控策略V3",
        version="v3.2.1",
        risk_level=RiskLevel.ROUTINE,
        description="个人消费贷常规策略迭代，优化反欺诈规则引擎",
        credit_product="个人消费贷",
        status=StrategyStatus.DRAFT,
        previous_stable_version="v3.1.0-stable",
    )
    storage.save_strategy(strategy)
    audit.log("创建策略", "管理员", "策略", strategy.strategy_id,
              f"创建策略 {strategy.name} {strategy.version}")

    print(f"\n📋 阶段1: 前置条件检查")
    strategy.status = StrategyStatus.PENDING_PRECHECK
    results = precheck.run_precheck(strategy)
    for r in results:
        icon = "✅" if r.passed else "❌"
        print(f"   {icon} {r.check_name}: {r.detail}")

    if not strategy.precheck_results.get("all_passed"):
        print("⚠️  前置检查随机模拟未通过，演示模式下自动调整为通过")
        strategy.precheck_results["all_passed"] = True
        strategy.status = StrategyStatus.PENDING_APPROVAL

    print(f"\n📋 阶段2: 生成审批流程")
    flow = approval.generate_approval_flow(strategy)
    print(f"   审批流程ID: {flow.flow_id}")
    for step in flow.steps:
        print(f"   步骤{step.step_order}: [{step.role.value}] {step.approver}")

    print(f"\n📋 阶段3: 自动审批")
    approval.simulate_approval(flow, auto_approve=True)
    for step in flow.steps:
        print(f"   ✅ [{step.role.value}] {step.approver} - {step.status}")

    print(f"\n📋 阶段4: 客群灰度推送")
    grayscale.init_grayscale(strategy)
    for seg_name in config.GRAYSCALE_ORDER:
        result = grayscale.advance_grayscale(strategy)
        advanced = result.get("advanced", [])
        for seg in advanced:
            ratio = strategy.grayscale_status.get(seg, {}).get("ratio", 0)
            print(f"   ✅ 推送至 [{seg}] 比例: {ratio*100:.0f}%")

    storage.save_strategy(strategy)
    for seg_name in config.GRAYSCALE_ORDER:
        storage.save_publish_record({
            "record_id": f"REC-{uuid.uuid4().hex[:8].upper()}",
            "strategy_id": strategy.strategy_id,
            "strategy_name": strategy.name,
            "version": strategy.version,
            "risk_level": strategy.risk_level.value,
            "credit_product": strategy.credit_product,
            "customer_segment": seg_name,
            "status": strategy.status.value,
            "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator": "管理员",
        })

    print(f"\n✅ 策略发布完成: {strategy.name} {strategy.version} [{strategy.status.value}]")

    print(f"\n{'='*60}")
    print("场景2: 实时风险监控 (模拟3个周期)")
    print("=" * 60)
    for i in range(3):
        result = monitor.monitor_cycle(strategy)
        snap = result["snapshot"]
        violations = result["violations"]
        icon = "✅" if not violations else "⚠️ "
        print(
            f"   [{snap['timestamp']}] {icon} "
            f"授信通过率: {snap['credit_approval_rate']*100:.2f}% | "
            f"欺诈识别率: {snap['fraud_detection_rate']*100:.2f}% | "
            f"放款延迟: {snap['loan_delay_seconds']:.1f}s | "
            f"逾期异常: {snap['overdue_anomaly_rate']*100:.2f}%"
        )
        if violations:
            for v in violations:
                print(f"   ⚠️  {v['metric']}: 当前值 {v['value']}, 阈值 {v['threshold']}")

    print(f"\n{'='*60}")
    print("场景3: 风险强制回滚 (模拟触发)")
    print("=" * 60)
    simulated_violations = [
        {"metric": "逾期异常", "value": 0.087, "threshold": {"max": 0.05, "direction": "max"}},
    ]
    record = rollback_mod.trigger_rollback(strategy, simulated_violations)
    report_text = rollback_mod.generate_rollback_report(record)
    print(f"\n{report_text}")
    rollback_mod.restore_previous_strategy(strategy)
    storage.save_strategy(strategy)

    print(f"\n{'='*60}")
    print("场景4: 风控策略回滚演练")
    print("=" * 60)
    strategy2 = RiskStrategy(
        strategy_id=f"STR-{uuid.uuid4().hex[:8].upper()}",
        name="紧急欺诈拦截策略V2",
        version="v2.5.0",
        risk_level=RiskLevel.EMERGENCY_FRAUD,
        description="针对新型团伙欺诈的紧急拦截策略",
        credit_product="经营性贷款",
        status=StrategyStatus.ACTIVE,
        previous_stable_version="v2.4.0-stable",
    )
    storage.save_strategy(strategy2)
    drill_record = drill_mod.create_drill(strategy2, operator="风控管理员")
    print(f"\n📋 演练计划:")
    for line in drill_record.plan.split("；"):
        print(f"   {line.strip()}")
    print(f"\n🔍 欺诈模拟校验: {drill_record.fraud_simulation_result}")
    print(f"🛡️ 风险处置结果: {drill_record.risk_disposal_result}")
    print(f"✅ 演练完成! ID: {drill_record.drill_id}")

    print(f"\n{'='*60}")
    print("场景5: 周报统计")
    print("=" * 60)
    stats = report_mod.generate_weekly_report(0)
    print(f"   发布成功率: {stats['publish_success_rate']*100:.2f}%")
    print(f"   回滚次数: {stats['rollback_count']}")
    print(f"   欺诈拦截率: {stats['avg_fraud_detection_rate']*100:.2f}%")
    print(f"   PDF报告: {stats['pdf_path']}")
    print(f"   Excel报表: {stats['excel_path']}")

    print(f"\n{'='*60}")
    print("场景6: 历史记录查询与导出")
    print("=" * 60)
    records = query_mod.query_publish_records()
    print(f"   查询到 {len(records)} 条发布记录")
    if records:
        filepath = query_mod.batch_export(records, export_format="csv")
        print(f"   已导出至: {filepath}")

    print(f"\n{'='*60}")
    print("场景7: 风险审计日志")
    print("=" * 60)
    logs = audit.query_logs()
    print(f"   审计日志共 {len(logs)} 条")
    for entry in logs[-10:]:
        print(f"   [{entry.get('timestamp')}] {entry.get('action')} | {entry.get('detail')[:70]}")

    print(f"\n{'='*60}")
    print("✅ 演示流程全部完成！")
    print(f"{'='*60}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(BANNER)
        print("使用方式:")
        print("  python main.py demo              运行完整演示")
        print("  python main.py create --name X    创建策略")
        print("  python main.py publish --strategy-id X  发布策略")
        print("  python main.py monitor --strategy-id X  实时监控")
        print("  python main.py drill --strategy-id X    回滚演练")
        print("  python main.py report             生成周报")
        print("  python main.py query              查询记录")
        print("  python main.py audit              审计日志")
        print("  python main.py list               策略列表")
    else:
        main()
