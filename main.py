#!/usr/bin/env python3
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import os
import uuid
import argparse
import time
import threading
import queue
from datetime import datetime, timedelta

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
from credit_risk_control import task_queue


BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║           银行信贷风控系统 - 策略发布与风险回滚管理           ║
║     Credit Risk Control: Strategy Publish & Rollback         ║
╚══════════════════════════════════════════════════════════════╝
"""

MONITOR_THREAD = None
MONITOR_STOP_EVENT = threading.Event()
DAEMON_TASK_QUEUE = queue.Queue()
args_noninteractive = False


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
    prev_stable = args.previous_version or "v1.0.0-stable"

    strategy = RiskStrategy(
        strategy_id=f"STR-{uuid.uuid4().hex[:8].upper()}",
        name=name,
        version=version,
        risk_level=risk_level,
        description=desc,
        credit_product=product,
        status=StrategyStatus.DRAFT,
        previous_stable_version=prev_stable,
    )

    storage.save_strategy(strategy)

    audit.log(
        action="创建策略",
        operator=args.operator or "管理员",
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=f"创建风控策略 {name} {version}，风险级别: {risk_level.value}，产品: {product}，基线: {prev_stable}",
    )

    print(f"\n✅ 策略创建成功")
    print(f"   策略ID: {strategy.strategy_id}")
    print(f"   名称: {strategy.name}")
    print(f"   版本: {strategy.version}")
    print(f"   风险级别: {strategy.risk_level.value}")
    print(f"   信贷产品: {strategy.credit_product}")
    print(f"   上一稳定版本: {strategy.previous_stable_version}")
    print(f"   状态: {strategy.status.value}")
    return strategy


def _run_manual_approval(flow, strategy, operator=None):
    operator_display = operator or flow.steps[flow.current_step].approver if flow.current_step < len(flow.steps) else "审批人"
    print(f"\n📋 进入人工逐步审批模式，共 {len(flow.steps)} 步")
    print(f"   当前操作人: {operator_display}")
    print(f"   (输入 p=通过, j=驳回, q=保存退出后续继续处理)")

    final_status = flow.status
    interrupted = False

    for i in range(len(flow.steps)):
        if flow.status in ("已通过", "已驳回"):
            break
        if flow.current_step >= len(flow.steps):
            break
        step = flow.steps[flow.current_step]
        print(f"\n   --- 步骤 {step.step_order}/{len(flow.steps)} ---")
        print(f"   角色: [{step.role.value}]")
        print(f"   审批人: {step.approver}")
        print(f"   已有状态: {step.status}")

        if step.status in ("已通过", "已驳回"):
            print(f"   (跳过已处理步骤)")
            flow.current_step += 1
            continue

        while True:
            choice = input(f"   {step.approver} 是否通过？(p=通过/j=驳回/q=保存退出): ").strip().lower()
            if choice in ("p", "j", "q"):
                break
            print("   无效输入，请输入 p/j/q")

        if choice == "q":
            interrupted = True
            print(f"   已保存进度后退出，当前处理到第 {step.step_order} 步，可从台账继续处理")
            break
        if choice == "p":
            comment = input("   审批备注(可选，直接回车跳过): ").strip() or "审批通过-人工"
            flow = approval.step_approve(flow, comment=comment, approved=True)
            print(f"   ✅ {step.approver} 通过审批")
        else:
            comment = input("   驳回原因: ").strip() or "不符合要求"
            flow = approval.step_approve(flow, comment=comment, approved=False)
            print(f"   ❌ {step.approver} 驳回审批: {comment}")
            break

    final_status = flow.status
    print(f"\n   审批流程最终状态: {final_status}" + (" (已保存进度，可继续)" if interrupted else ""))
    return flow, interrupted


def publish_strategy(args):
    strategy_id = args.strategy_id
    strategy_dict = storage.load_strategy(strategy_id)
    if not strategy_dict:
        print(f"❌ 未找到策略: {strategy_id}")
        return None

    strategy = _dict_to_strategy(strategy_dict)
    approval_mode = getattr(args, "approval_mode", "auto")

    print(f"\n{'='*60}")
    print(f"策略发布流程启动: {strategy.name} {strategy.version}")
    print(f"风险级别: {strategy.risk_level.value} | 审批模式: {'自动审批' if approval_mode == 'auto' else '人工逐步审批'}")
    print(f"{'='*60}")

    print(f"\n📋 阶段1: 前置条件检查...")
    strategy.status = StrategyStatus.PENDING_PRECHECK
    results = precheck.run_precheck(strategy)

    for r in results:
        status_icon = "✅" if r.passed else "❌"
        print(f"   {status_icon} {r.check_name}: {r.detail}")

    if not strategy.precheck_results.get("all_passed"):
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

    print(f"\n📋 阶段2: 生成/获取信贷合规审批流程...")
    existing_flows = approval.get_flow_by_strategy(strategy.strategy_id)
    flow = None
    for f in existing_flows:
        if f.status in ("待审批", "审批中"):
            flow = f
            print(f"   找到未完成的审批流程: {flow.flow_id}，将继续处理")
            break
    if not flow:
        flow = approval.generate_approval_flow(strategy)
        print(f"   已新建审批流程: {flow.flow_id}")

    flow.status = "审批中"
    approval.persist_flow(flow)
    strategy.status = StrategyStatus.PENDING_APPROVAL
    storage.save_strategy(strategy)

    print(f"   审批流程ID: {flow.flow_id}")
    for step in flow.steps:
        icon = {"待审批": "⏳", "已通过": "✅", "已驳回": "❌"}.get(step.status, "❓")
        print(f"   步骤{step.step_order}: {icon} [{step.role.value}] {step.approver} - {step.status}")

    print(f"\n📋 阶段3: 审批流程")
    interrupted = False
    if approval_mode == "auto":
        flow = approval.simulate_approval(flow, auto_approve=True)
        for step in flow.steps:
            icon = "✅" if step.status == "已通过" else "❌"
            print(f"   {icon} [{step.role.value}] {step.approver} - {step.status} ({step.approved_at})")
    else:
        flow, interrupted = _run_manual_approval(flow, strategy)

    if interrupted:
        strategy.status = StrategyStatus.PENDING_APPROVAL
        strategy.approval_flow = flow
        storage.save_strategy(strategy)
        audit.log(
            action="审批保存退出",
            operator="用户",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} {strategy.version} 审批流程 {flow.flow_id} 中途保存退出，当前进度 step {flow.current_step+1}/{len(flow.steps)}",
        )
        print(f"\n💾 审批进度已保存，后续可通过 'python main.py ledger --continue --flow-id {flow.flow_id}' 或 'python main.py publish --strategy-id {strategy.strategy_id} --approval-mode manual' 继续")
        return strategy

    if flow.status == "已驳回":
        strategy.status = StrategyStatus.APPROVAL_REJECTED
        storage.save_strategy(strategy)
        audit.log(
            action="审批驳回",
            operator="用户",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} {strategy.version} 审批流程 {flow.flow_id} 被驳回",
        )
        print(f"\n❌ 审批被驳回，策略发布终止")
        return strategy

    if flow.status != "已通过":
        strategy.status = StrategyStatus.PENDING_APPROVAL
        storage.save_strategy(strategy)
        audit.log(
            action="审批待继续",
            operator="系统",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} {strategy.version} 审批流程 {flow.flow_id} 状态: {flow.status}，待继续",
        )
        print(f"\n⏳ 审批未完成（{flow.status}），请从台账继续处理")
        return strategy

    audit.log(
        action="审批通过",
        operator="系统",
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=f"策略 {strategy.name} {strategy.version} 审批流程 {flow.flow_id} 已通过",
    )

    print(f"\n📋 阶段4: 客群灰度策略推送...")
    grayscale.init_grayscale(strategy)

    for _ in config.GRAYSCALE_ORDER:
        result = grayscale.advance_grayscale(strategy, monitor_check_func=monitor.is_strategy_healthy)
        if result.get("rolled_back"):
            print(f"\n⚠️  灰度推送过程中监控指标异常，触发自动回滚！")
            _handle_rollback(strategy, [])
            storage.save_strategy(strategy)
            return strategy
        for seg in result.get("advanced", []):
            ratio = strategy.grayscale_status.get(seg, {}).get("ratio", 0)
            print(f"   ✅ 推送至 [{seg}] 比例: {ratio*100:.0f}%")
        if result.get("completed"):
            print(f"   ✅ 全量灰度发布完成！")

    storage.save_strategy(strategy)

    for segment_name in config.GRAYSCALE_ORDER:
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

    if getattr(args, "monitor", False):
        start_monitoring(strategy)

    return strategy


def _handle_rollback(strategy, violations):
    if not violations:
        violations = [{"metric": "模拟触发", "value": 0, "threshold": {"max": 0.05, "direction": "max"}}]
    record = rollback_mod.trigger_rollback(strategy, violations)
    report_text = rollback_mod.generate_rollback_report(record)
    print(f"\n{report_text}")

    rollback_mod.restore_previous_strategy(strategy)
    storage.save_strategy(strategy)

    print(f"\n✅ 已自动恢复至上一稳定风控策略 {strategy.version} [{strategy.status.value}]，重启实时风险监控")


def start_monitoring(strategy):
    global MONITOR_THREAD, MONITOR_STOP_EVENT

    MONITOR_STOP_EVENT.clear()
    print(f"\n🔍 启动实时风险监控 (每{config.MONITOR_INTERVAL_SECONDS}秒检查一次)")
    print(f"   策略: {strategy.name} {strategy.version}")
    print(f"   阈值: 授信通过率60%-95% | 欺诈识别率≥80% | 放款延迟≤300s | 逾期异常≤5%")
    print(f"   按 Ctrl+C 停止监控\n")

    cycle_count = 0
    try:
        while not MONITOR_STOP_EVENT.is_set():
            cycle_count += 1
            result = monitor.monitor_cycle(strategy)
            snap = result["snapshot"]
            violations = result["violations"]

            status_icons = "✅" if not violations else "⚠️ "
            print(
                f"  [{snap['timestamp']}] {status_icons} "
                f"通过率 {snap['credit_approval_rate']*100:>5.2f}% | "
                f"欺诈识别 {snap['fraud_detection_rate']*100:>5.2f}% | "
                f"延迟 {snap['loan_delay_seconds']:>6.1f}s | "
                f"逾期 {snap['overdue_anomaly_rate']*100:>5.2f}%"
            )

            if violations:
                print(f"\n  ⚠️  检测到风险指标超阈值！立即触发强制回滚...")
                for v in violations:
                    print(f"     - {v['metric']}: 当前值 {v['value']}")

                audit.log(
                    action="监控告警",
                    operator="系统",
                    target_type="策略",
                    target_id=strategy.strategy_id,
                    detail=f"监控指标超阈值: {', '.join(v['metric'] for v in violations)}，触发强制回滚",
                )

                _handle_rollback(strategy, violations)
                print(f"\n  ⏹  监控线程因回滚终止")
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
    for i, line in enumerate(record.plan.split("；"), 1):
        print(f"   {i}. {line.strip()}")
    print(f"\n🔍 欺诈模拟校验:")
    print(f"   {record.fraud_simulation_result}")
    print(f"\n🛡️ 风险处置结果:")
    print(f"   {record.risk_disposal_result}")
    print(f"\n✅ 演练完成！演练ID: {record.drill_id}")


def generate_report(args):
    week_offset = args.week_offset or 0
    print(f"\n{'='*60}")
    print(f"生成信贷风控周报")
    print(f"{'='*60}")

    stats = report_mod.generate_weekly_report(week_offset)

    print(f"\n📊 周报统计 (周期: {stats['period']})")
    print(f"   策略发布总数:   {stats['total_publishes']}")
    print(f"   发布成功数:     {stats['success_publishes']}")
    print(f"   发布成功率:     {stats['publish_success_rate']*100:.2f}%")
    print(f"   回滚次数:       {stats['rollback_count']}")
    print(f"   平均欺诈拦截率: {stats['avg_fraud_detection_rate']*100:.2f}%")

    print(f"\n📈 每日风险趋势:")
    print(f"   {'日期':<12} {'通过率':<8} {'欺诈识别':<8} {'逾期异常':<8} {'发布':<4} {'回滚':<4}")
    for day in stats["trend_data"]:
        print(
            f"   {day['date']:<12} "
            f"{day['credit_approval_rate']*100:>6.2f}%  "
            f"{day['fraud_detection_rate']*100:>6.2f}%  "
            f"{day['overdue_anomaly_rate']*100:>6.2f}%  "
            f"{day['publish_count']:>4} "
            f"{day['rollback_count']:>4}"
        )

    print(f"\n📦 各产品发布分布:")
    for product, cnt in stats["product_distribution"].items():
        print(f"   {product}: {cnt} 次")

    print(f"\n🎯 各客群风险对比:")
    for seg, v in stats["segment_risk"].items():
        print(
            f"   {seg:<8} 发布{v['publish_count']:>2}次, 回滚{v['rollback_count']}次, "
            f"欺诈识别{v['avg_fraud_rate']*100:.2f}%, 逾期{v['avg_overdue_rate']*100:.2f}%"
        )

    print(f"\n📄 报告文件:")
    print(f"   📕 PDF报告:  {stats['pdf_path']}")
    print(f"   📗 Excel表: {stats['excel_path']}")
    return stats


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
        print(f"\n   ┌ 记录ID: {r.get('record_id')}")
        print(f"   │ 策略: {r.get('strategy_name')} {r.get('version')}")
        print(f"   │ 风险级别: {r.get('risk_level')}")
        print(f"   │ 信贷产品: {r.get('credit_product')}")
        print(f"   │ 客群: {r.get('customer_segment')}")
        print(f"   │ 状态: {r.get('status')}")
        print(f"   └ 发布时间: {r.get('publish_time')}")

    if args.export:
        fmt = args.export_format or "csv"
        filepath = query_mod.batch_export(
            records,
            export_format=fmt,
            publish_time_start=args.time_start,
            publish_time_end=args.time_end,
        )
        print(f"\n📤 合规打包已导出至: {filepath}")
        print(f"   内含: 发布记录+回滚记录+审批记录+审计日志")


def show_audit_logs(args):
    logs = audit.query_logs(date=args.date, action=args.action, target_id=args.target_id)

    print(f"\n{'='*60}")
    print(f"风险审计日志 (共 {len(logs)} 条)")
    print(f"{'='*60}")

    for entry in logs[-100:]:
        print(
            f"   [{entry.get('timestamp')}] {entry.get('action'):<10} | "
            f"{entry.get('operator'):<10} | {entry.get('detail')[:85]}"
        )

    if len(logs) > 100:
        print(f"\n   ... 仅显示最近 100 条，共 {len(logs)} 条记录")


def list_strategies(args):
    strategies = storage.load_all_strategies()

    print(f"\n{'='*80}")
    print(f"{'策略ID':<14} {'名称':<26} {'版本':<14} {'风险级别':<12} {'产品':<12} {'状态':<10}")
    print(f"{'─'*80}")

    if not strategies:
        print("   暂无策略")
        return

    for s in strategies:
        print(
            f"{s.get('strategy_id'):<14} "
            f"{s.get('name')[:24]:<26} "
            f"{s.get('version'):<14} "
            f"{s.get('risk_level'):<12} "
            f"{s.get('credit_product'):<12} "
            f"{s.get('status'):<10}"
        )

    print(f"\n共 {len(strategies)} 条策略记录")


def _continue_publish_after_approval(strategy: RiskStrategy, operator: str = "系统") -> RiskStrategy:
    """审批通过后，继续完成灰度发布、全量发布、监控等流程"""
    audit.log(
        action="审批通过",
        operator=operator,
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=f"策略 {strategy.name} {strategy.version} 审批通过，进入灰度发布",
    )
    print(f"\n✅ 审批通过!")

    print(f"\n📋 阶段4: 客群灰度发布")
    grayscale_result = grayscale.execute_grayscale_release(strategy)
    print(f"   灰度发布结果: {grayscale_result.get('status')}")
    for step in grayscale_result.get("steps", []):
        print(f"   • {step}")

    if not grayscale_result.get("success"):
        strategy.status = StrategyStatus.ROLLED_BACK
        storage.save_strategy(strategy)
        audit.log(
            action="灰度发布失败",
            operator="系统",
            target_type="策略",
            target_id=strategy.strategy_id,
            detail=f"策略 {strategy.name} {strategy.version} 灰度发布失败",
        )
        print(f"\n❌ 灰度发布失败，策略终止")
        return strategy

    print(f"\n📋 阶段5: 全量发布")
    strategy.status = StrategyStatus.FULL_PUBLISH
    print(f"   已向所有客群推送: {strategy.name} {strategy.version}")

    publish_record = {
        "record_id": f"PUB-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "strategy_id": strategy.strategy_id,
        "strategy_name": strategy.name,
        "version": strategy.version,
        "risk_level": strategy.risk_level.value,
        "credit_product": strategy.credit_product,
        "customer_segment": "全客群",
        "status": "已发布",
        "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "operator": operator,
        "rollback_count": 0,
    }
    existing = _load_json(config.PUBLISH_RECORDS_FILE)
    existing.append(publish_record)
    _save_json(existing, config.PUBLISH_RECORDS_FILE)

    audit.log(
        action="全量发布",
        operator=operator,
        target_type="策略",
        target_id=strategy.strategy_id,
        detail=f"策略 {strategy.name} {strategy.version} 全量发布成功",
    )

    strategy.status = StrategyStatus.ACTIVE
    storage.save_strategy(strategy)

    print(f"\n✅ 策略发布成功!")
    print(f"   策略: {strategy.name} {strategy.version}")
    print(f"   状态: {strategy.status.value}")
    print(f"   授信产品: {strategy.credit_product}")
    print(f"   风险级别: {strategy.risk_level.value}")
    return strategy


def approval_ledger(args):
    if args.continue_flow:
        flow_id = args.flow_id
        if not flow_id:
            print("❌ 请使用 --flow-id 指定要继续的审批流程ID")
            return
        flows = approval._load_flows()
        flow_dict = None
        for f in flows:
            if f["flow_id"] == flow_id:
                flow_dict = f
                break
        if not flow_dict:
            print(f"❌ 未找到审批流程: {flow_id}")
            return
        flow = approval._dict_to_flow(flow_dict)
        strategy_dict = storage.load_strategy(flow.strategy_id)
        if not strategy_dict:
            print(f"❌ 未找到策略: {flow.strategy_id}")
            return
        strategy = _dict_to_strategy(strategy_dict)
        print(f"\n🔄 继续审批流程: {flow_id}")
        print(f"   策略: {strategy.name} {strategy.version}")
        print(f"   当前步骤: {flow.current_step}")
        operator = args.operator or flow.steps[flow.current_step].approver
        if operator:
            print(f"   操作人: {operator}")

        flow, interrupted = _run_manual_approval(flow, strategy, operator=operator)
        approval.persist_flow(flow)

        if interrupted:
            strategy.status = StrategyStatus.PENDING_APPROVAL
            storage.save_strategy(strategy)
            print(f"\n⏸️  审批已保存进度，下次可通过 ledger --continue --flow-id {flow_id} 继续")
            audit.log(
                action="审批保存退出",
                operator=operator or "用户",
                target_type="审批流程",
                target_id=flow_id,
                detail=f"审批流程已保存，当前步骤 {flow.current_step}，可继续处理",
            )
        elif flow.status == "已驳回":
            strategy.status = StrategyStatus.APPROVAL_REJECTED
            storage.save_strategy(strategy)
            approval.persist_flow(flow)
            print(f"\n❌ 审批被驳回，策略状态: {strategy.status.value}")
            audit.log(
                action="审批未通过",
                operator=operator or "用户",
                target_type="审批流程",
                target_id=flow_id,
                detail=f"审批流程 {flow_id} 被驳回，策略 {strategy.strategy_id}",
            )
        else:
            if strategy.status == StrategyStatus.PENDING_APPROVAL:
                print(f"\n✅ 审批已完成，继续发布流程")
                strategy.status = StrategyStatus.GRAYSCALE_DEPLOYING
                storage.save_strategy(strategy)
                approval.persist_flow(flow)
                published = _continue_publish_after_approval(strategy, operator=operator)
                if published:
                    print(f"\n✅ 发布成功! 策略 {published.name} 状态: {published.status.value}")
                else:
                    print(f"\n⚠️  发布流程未完成")
            storage.save_strategy(strategy)
            approval.persist_flow(flow)

        return

    approver = args.approver
    role = args.role
    risk_level = args.risk_level
    status = args.status
    ledger_type = args.type

    if ledger_type == "todo":
        records = approval.list_pending_tasks(approver=approver, role=role)
        title = "待办审批"
    elif ledger_type == "done":
        records = approval.list_completed_approvals(approver=approver, role=role)
        title = "已通过审批"
    elif ledger_type == "rejected":
        records = approval.list_rejected_approvals(approver=approver, role=role)
        title = "已驳回审批"
    else:
        records = approval.query_approval_ledger(
            approver=approver, role=role, risk_level=risk_level, status=status, ledger_type=ledger_type
        )
        title = "审批台账"

    if risk_level:
        records = [r for r in records if r.get("risk_level") == risk_level]

    print(f"\n{'='*80}")
    print(f"策略审批{title} - 共 {len(records)} 条")
    print(f"筛选条件: 审批人={approver or 'ALL'} | 角色={role or 'ALL'} | 风险级别={risk_level or 'ALL'} | 状态={status or 'ALL'}")
    print(f"{'─'*80}")

    if not records:
        print("   暂无记录")
        return

    for r in records:
        print(f"\n 📌 流程ID: {r['flow_id']}  策略ID: {r['strategy_id']}  风险:{r['risk_level']}  状态:{r['status']}")
        for step in r.get("steps", []):
            icon = {"待审批": "⏳", "已通过": "✅", "已驳回": "❌"}.get(step.get("status"), "❓")
            print(
                f"    {icon} 步骤{step.get('step_order')} [{step.get('role')}] {step.get('approver')} "
                f"- {step.get('status')}"
                + (f" ({step.get('approved_at')})" if step.get("approved_at") else "")
            )
            if step.get("comment"):
                print(f"        备注: {step.get('comment')}")


def _next_monday_report_time(now: datetime = None) -> datetime:
    now = now or datetime.now()
    days_ahead = (0 - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= 9:
        days_ahead = 7
    target = (now + timedelta(days=days_ahead)).replace(hour=9, minute=0, second=0, microsecond=0)
    return target


def daemon_mode(args):
    print(BANNER)
    print("🛡️  守护模式启动 - 银行信贷风控自动化值守")
    print(f"   启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   监控间隔: {config.MONITOR_INTERVAL_SECONDS}秒")
    print(f"   按 Ctrl+C 安全退出\n")

    next_report_dt = _next_monday_report_time()
    last_report_info = {"time": "暂未生成", "result": "N/A", "pdf": "-", "excel": "-"}

    last_status_line = ""

    def _print_status(snapshot_info: str = ""):
        nonlocal last_report_info, last_status_line
        now = datetime.now()
        until_report = next_report_dt - now
        hours, rem = divmod(int(until_report.total_seconds()), 3600)
        mins = rem // 60

        # count active strategies
        strats = storage.load_all_strategies()
        active_count = sum(1 for s in strats if s.get("status") in ("全量发布", "生效中"))

        new_line = (
            f"\r"
            f"📋 下周一9点周报: {next_report_dt.strftime('%Y-%m-%d %H:%M')} (还剩{hours}h{mins}m) | "
            f"📈 最近周报: {last_report_info['time']} {last_report_info['result']} | "
            f"🎯 监控策略数: {active_count}"
            + (f" | {snapshot_info}" if snapshot_info else "")
        )
        if new_line != last_status_line:
            sys.stdout.write(new_line + " " * 20)
            sys.stdout.flush()
            last_status_line = new_line

    def _process_daemon_tasks():
        nonlocal next_report_dt, last_report_info
        pending_tasks = task_queue.poll_pending_tasks()
        for filepath, task in pending_tasks:
            task_queue.mark_task_running(filepath, task)
            task_type = task.get("type")
            payload = task.get("payload", {})
            print(f"\n📢 [守护线程] 收到任务 [{task['task_id']}] {task_type}")

            result = None
            error = None
            try:
                if task_type == "publish":
                    strategy_id = payload.get("strategy_id")
                    print(f"   处理发布任务: {strategy_id}")
                    args_obj = argparse.Namespace(
                        strategy_id=strategy_id,
                        approval_mode=payload.get("approval_mode", "auto"),
                        monitor=payload.get("monitor", False),
                        operator="守护进程",
                    )
                    published = publish_strategy(args_obj)
                    failure_statuses = {
                        StrategyStatus.PRECHECK_FAILED,
                        StrategyStatus.APPROVAL_REJECTED,
                        StrategyStatus.ROLLED_BACK,
                    }
                    if published is None:
                        result = {
                            "strategy_id": strategy_id,
                            "status": "失败",
                            "_business_failed": True,
                            "_failure_reason": "前置检查未通过，策略发布终止",
                        }
                    elif published.status in failure_statuses:
                        result = {
                            "strategy_id": strategy_id,
                            "status": published.status.value,
                            "_business_failed": True,
                            "_failure_reason": f"策略发布失败，阶段: {published.status.value}",
                        }
                    else:
                        result = {
                            "strategy_id": strategy_id,
                            "status": published.status.value,
                        }
                    print(f"   发布任务完成，状态: {published.status.value if published else '失败'}")

                elif task_type == "manual_report":
                    print(f"   执行手动生成周报...")
                    stats = report_mod.generate_weekly_report(0)
                    last_report_info = {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "result": f"发布{stats['publish_success_rate']*100:.1f}%",
                        "pdf": stats["pdf_path"],
                        "excel": stats["excel_path"],
                    }
                    result = {
                        "publish_success_rate": stats["publish_success_rate"],
                        "rollback_count": stats["rollback_count"],
                        "avg_fraud_rate": stats["avg_fraud_detection_rate"],
                        "pdf_path": stats["pdf_path"],
                        "excel_path": stats["excel_path"],
                    }
                    print(f"   周报生成完成: {result['pdf_path']}")

            except Exception as ex:
                error = f"{type(ex).__name__}: {str(ex)}"
                print(f"   任务失败: {error}")
                import traceback
                traceback.print_exc()

            task_queue.complete_task(filepath, task, result, error)
            result_text = "✅ 成功" if not error else "❌ 失败"
            print(f"   任务 {task['task_id']} {result_text}")

    def _try_generate_weekly():
        nonlocal next_report_dt, last_report_info
        now = datetime.now()
        if now >= next_report_dt:
            print(f"\n📊 [守护线程] 触发每周一9点周报生成...")
            try:
                stats = report_mod.generate_weekly_report(0)
                last_report_info = {
                    "time": now.strftime("%H:%M:%S"),
                    "result": f"发布{stats['publish_success_rate']*100:.1f}%",
                    "pdf": stats["pdf_path"],
                    "excel": stats["excel_path"],
                }
                print(f"✅ [守护线程] 周报已生成: {last_report_info['pdf']}")
            except Exception as ex:
                print(f"⚠️  [守护线程] 周报生成异常: {ex}")
            next_report_dt = _next_monday_report_time(now)

    def _monitor_all_active():
        active_strats = storage.load_all_strategies()
        results_info = []
        for s_dict in active_strats:
            if s_dict.get("status") not in ("全量发布", "生效中"):
                continue
            strategy = _dict_to_strategy(s_dict)
            result = monitor.monitor_cycle(strategy)
            snap = result["snapshot"]
            violations = result["violations"]
            if violations:
                print(f"\n⚠️  [守护线程] 策略 {strategy.name} {strategy.version} 指标超阈值，强制回滚!")
                audit.log(
                    action="监控告警",
                    operator="守护线程",
                    target_type="策略",
                    target_id=strategy.strategy_id,
                    detail=f"监控指标超阈值: {', '.join(v['metric'] for v in violations)}，触发强制回滚",
                )
                _handle_rollback(strategy, violations)
            else:
                results_info.append(f"{strategy.name[:8]}:✅")
        return " ".join(results_info)

    try:
        cycle = 0
        while True:
            cycle += 1
            _process_daemon_tasks()
            _try_generate_weekly()
            snapshot_info = _monitor_all_active()
            _print_status(snapshot_info)
            for _ in range(10):
                if MONITOR_STOP_EVENT.is_set():
                    break
                time.sleep(config.MONITOR_INTERVAL_SECONDS / 10)
            if MONITOR_STOP_EVENT.is_set():
                break
    except KeyboardInterrupt:
        pass

    MONITOR_STOP_EVENT.set()
    print(f"\n\n🛑 守护模式已安全退出，共运行 {cycle} 个周期")
    print(f"   最近周报: {last_report_info['time']}  {last_report_info['result']}")


def submit_daemon_publish(args):
    strategy_id = args.strategy_id
    s = storage.load_strategy(strategy_id)
    if not s:
        print(f"❌ 未找到策略 {strategy_id}")
        return
    task = task_queue.submit_publish_task(strategy_id, operator=args.operator or "用户")
    print(f"✅ 已向守护进程提交发布任务")
    print(f"   任务ID: {task['task_id']}")
    print(f"   策略: {s.get('name')} {s.get('version')}")
    print(f"   提交时间: {task['submitted_at']}")
    print("   (请确保守护进程正在运行: python main.py daemon)")


def manage_tasks(args):
    if getattr(args, "cancel", None):
        task_id = args.cancel
        result = task_queue.cancel_task(task_id)
        if result.get("success"):
            print(f"✅ 任务 {task_id} 已取消")
        else:
            print(f"❌ 取消失败: {result.get('error', '未知错误')}")
        return

    if getattr(args, "retry", None):
        task_id = args.retry
        result = task_queue.retry_task(task_id, operator=args.operator or "用户")
        if result.get("success"):
            new_task = result["new_task"]
            print(f"✅ 已重试任务，新任务ID: {new_task['task_id']}")
            print(f"   原任务: {result['original_task_id']}")
            print(f"   提交时间: {new_task['submitted_at']}")
        else:
            print(f"❌ 重试失败: {result.get('error', '未知错误')}")
        return

    if getattr(args, "detail", None):
        task_id = args.detail
        task = task_queue.get_task_detail(task_id)
        if not task:
            print(f"❌ 未找到任务: {task_id}")
            return
        print(f"\n{'='*60}")
        print(f"任务详情: {task_id}")
        print(f"{'='*60}")
        print(f"   任务ID:     {task.get('task_id')}")
        print(f"   类型:       {task.get('type')}")
        print(f"   状态:       {task.get('status')}")
        print(f"   操作人:     {task.get('operator', 'N/A')}")
        print(f"   提交时间:   {task.get('submitted_at', 'N/A')}")
        print(f"   开始时间:   {task.get('started_at', 'N/A')}")
        print(f"   完成时间:   {task.get('completed_at', 'N/A')}")
        started = task.get('started_at')
        completed = task.get('completed_at')
        if started and completed:
            try:
                dt_s = datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                dt_c = datetime.strptime(completed, "%Y-%m-%d %H:%M:%S")
                duration = dt_c - dt_s
                print(f"   执行耗时:   {duration}")
            except ValueError:
                print(f"   执行耗时:   无法计算")
        if task.get('result'):
            print(f"   结果:       {task['result']}")
        if task.get('error'):
            print(f"   错误:       {task['error']}")
        if task.get('parent_task_id'):
            print(f"   父任务ID:   {task['parent_task_id']}")

        logs = task_queue.get_task_audit_logs(task_id)
        if logs:
            print(f"\n   相关审计日志 ({len(logs)} 条):")
            for entry in logs:
                print(f"      [{entry.get('timestamp')}] {entry.get('action')} - {entry.get('detail', '')[:80]}")
        return

    tasks = task_queue.list_tasks(
        status_filter=getattr(args, "status", None),
        task_type=getattr(args, "type", None),
        operator=getattr(args, "operator", None),
        strategy_id=getattr(args, "strategy_id", None),
        time_start=getattr(args, "time_start", None),
        time_end=getattr(args, "time_end", None),
    )

    status_counts = {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    for t in tasks:
        s = t.get("status", "pending")
        if s in status_counts:
            status_counts[s] += 1

    print(f"\n{'='*80}")
    print(f"任务列表 (共 {len(tasks)} 条)")
    print(f"状态统计: 待处理={status_counts['pending']} 运行中={status_counts['running']} "
          f"已完成={status_counts['done']} 失败={status_counts['failed']} 已取消={status_counts['cancelled']}")
    print(f"{'─'*80}")

    if not tasks:
        print("   暂无任务")
        return

    for t in tasks:
        status_icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "cancelled": "🚫"}.get(t.get("status"), "❓")
        strategy_info = ""
        payload = t.get("payload", {})
        if payload.get("strategy_id"):
            strategy_info = f" 策略:{payload['strategy_id']}"
        print(
            f"  {status_icon} {t['task_id']:<28} 类型:{t.get('type', 'N/A'):<14} "
            f"状态:{t.get('status'):<10} 操作人:{t.get('operator', 'N/A'):<10} "
            f"提交:{t.get('submitted_at', 'N/A')}{strategy_info}"
        )


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


def main():
    global args_noninteractive
    parser = argparse.ArgumentParser(
        description="银行信贷风控系统 - 策略发布与风险回滚自动化管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "\n使用示例:\n"
            "  python main.py demo                         # 完整演示\n"
            "  python main.py create --name 策略X           # 创建策略\n"
            "  python main.py publish --strategy-id X --approval-mode manual  # 人工审批发布\n"
            "  python main.py ledger --type todo --approver 张伟  # 张伟的待办审批\n"
            "  python main.py daemon                         # 守护值守模式\n"
            "  python main.py query --product 个人消费贷 --export  # 筛选并打包导出\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    create_parser = subparsers.add_parser("create", help="创建风控策略")
    create_parser.add_argument("--name", required=True, help="策略名称")
    create_parser.add_argument("--version", help="策略版本号(自动补v前缀)")
    create_parser.add_argument("--risk-level", choices=["常规", "紧急", "监管"], default="常规", help="风险级别")
    create_parser.add_argument("--product", choices=config.CREDIT_PRODUCTS, help="信贷产品")
    create_parser.add_argument("--description", help="策略描述")
    create_parser.add_argument("--previous-version", help="上一稳定版本(回滚目标)")
    create_parser.add_argument("--operator", help="操作人")

    publish_parser = subparsers.add_parser("publish", help="发布风控策略(前置检查→审批→灰度→监控)")
    publish_parser.add_argument("--strategy-id", required=True, help="策略ID")
    publish_parser.add_argument("--approval-mode", choices=["auto", "manual"], default="auto", help="审批模式(默认自动)")
    publish_parser.add_argument("--monitor", action="store_true", help="发布后立即启动实时监控")
    publish_parser.add_argument("--operator", help="操作人")

    monitor_parser = subparsers.add_parser("monitor", help="启动单策略实时风险监控")
    monitor_parser.add_argument("--strategy-id", required=True, help="策略ID")

    drill_parser = subparsers.add_parser("drill", help="风控策略回滚演练")
    drill_parser.add_argument("--strategy-id", required=True, help="策略ID")
    drill_parser.add_argument("--operator", help="操作人")

    report_parser = subparsers.add_parser("report", help="生成周报(PDF风险趋势图+Excel报表)")
    report_parser.add_argument("--week-offset", type=int, default=0, help="周偏移量(0=本周,1=上周)")

    query_parser = subparsers.add_parser("query", help="查询+打包导出发布/回滚/审批/审计记录")
    query_parser.add_argument("--time-start", help="开始时间 YYYY-MM-DD")
    query_parser.add_argument("--time-end", help="结束时间 YYYY-MM-DD")
    query_parser.add_argument("--product", choices=config.CREDIT_PRODUCTS, help="信贷产品")
    query_parser.add_argument("--segment", choices=config.GRAYSCALE_ORDER, help="客群类型")
    query_parser.add_argument("--version", help="版本号(模糊匹配)")
    query_parser.add_argument("--export", action="store_true", help="打包导出4类记录为ZIP")
    query_parser.add_argument("--export-format", choices=["csv", "json"], default="csv", help="ZIP内文件格式")

    audit_parser = subparsers.add_parser("audit", help="查看风险审计日志(全程留痕)")
    audit_parser.add_argument("--date", help="日期 YYYY-MM-DD")
    audit_parser.add_argument("--action", help="操作类型筛选")
    audit_parser.add_argument("--target-id", help="目标ID(策略/流程/导出ID)")

    subparsers.add_parser("list", help="列出所有策略(可检查回滚后的版本和状态)")

    ledger_parser = subparsers.add_parser("ledger", help="审批台账(待办/已办/驳回+多维度筛选)")
    ledger_parser.add_argument("--type", choices=["todo", "done", "rejected", "all"], default="all", help="台账类型")
    ledger_parser.add_argument("--approver", help="审批人姓名(如 张伟)")
    ledger_parser.add_argument("--role", choices=["风控", "授信", "法务", "合规"], help="审批角色")
    ledger_parser.add_argument("--risk-level", choices=["常规策略迭代", "紧急欺诈拦截", "监管风控整改"], help="风险级别")
    ledger_parser.add_argument("--status", choices=["待审批", "已通过", "已驳回"], help="审批状态")
    ledger_parser.add_argument("--continue", dest="continue_flow", action="store_true", help="继续未完成的审批流程")
    ledger_parser.add_argument("--flow-id", help="指定审批流程ID(配合--continue使用)")
    ledger_parser.add_argument("--operator", help="当前操作人姓名(用于继续审批时)")

    daemon_parser = subparsers.add_parser("daemon", help="守护值守模式(自动监控+定时周报+任务处理)")
    daemon_parser.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)

    submit_parser = subparsers.add_parser("submit", help="向守护进程提交发布任务")
    submit_parser.add_argument("--strategy-id", required=True, help="策略ID")
    submit_parser.add_argument("--operator", help="操作人姓名")

    manual_report_parser = subparsers.add_parser("manual-report", help="在守护进程中手动触发生成周报")
    manual_report_parser.add_argument("--operator", help="操作人姓名")

    tasks_parser = subparsers.add_parser("tasks", help="任务管理(查看/取消/重试)")
    tasks_parser.add_argument("--status", choices=["pending", "running", "done", "failed", "cancelled"], help="按状态筛选")
    tasks_parser.add_argument("--type", choices=["publish", "manual_report"], dest="type", help="按任务类型筛选")
    tasks_parser.add_argument("--operator", help="按操作人筛选")
    tasks_parser.add_argument("--strategy-id", help="按策略ID筛选")
    tasks_parser.add_argument("--time-start", help="开始时间 YYYY-MM-DD")
    tasks_parser.add_argument("--time-end", help="结束时间 YYYY-MM-DD")
    tasks_parser.add_argument("--detail", help="查看任务详情(传入任务ID)")
    tasks_parser.add_argument("--cancel", help="取消待处理任务(传入任务ID)")
    tasks_parser.add_argument("--retry", help="重试失败任务(传入任务ID)")

    demo_parser = subparsers.add_parser("demo", help="一键完整演示(7大场景)")
    demo_parser.add_argument("--non-interactive", action="store_true", help="非交互模式")

    args = parser.parse_args()

    if args.command == "demo":
        args_noninteractive = True
        run_demo()
    elif args.command == "create":
        create_strategy(args)
    elif args.command == "publish":
        publish_strategy(args)
    elif args.command == "monitor":
        s_dict = storage.load_strategy(args.strategy_id)
        if s_dict:
            s = _dict_to_strategy(s_dict)
            start_monitoring(s)
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
    elif args.command == "ledger":
        approval_ledger(args)
    elif args.command == "daemon":
        MONITOR_STOP_EVENT.clear()
        daemon_mode(args)
    elif args.command == "submit":
        submit_daemon_publish(args)
    elif args.command == "manual-report":
        task = task_queue.submit_manual_report_task(operator=args.operator or "用户")
        print(f"✅ 已向守护进程提交手动周报任务")
        print(f"   任务ID: {task['task_id']}")
        print(f"   提交时间: {task['submitted_at']}")
        print("   (请确保守护进程正在运行: python main.py daemon)")
    elif args.command == "tasks":
        manage_tasks(args)
    else:
        print(BANNER)
        parser.print_help()


def run_demo():
    global args_noninteractive
    args_noninteractive = True

    print(BANNER)
    print("🚀 启动完整演示流程 (7大场景)\n")

    print("=" * 80)
    print("场景1: 常规策略迭代 - 完整发布流程 (自动审批)")
    print("=" * 80)

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

    strategy.status = StrategyStatus.PENDING_PRECHECK
    results = precheck.run_precheck(strategy)
    all_ok = strategy.precheck_results.get("all_passed", True)
    for r in results:
        icon = "✅" if r.passed else "❌"
        print(f"   {icon} {r.check_name}: {r.detail}")
    if not all_ok:
        strategy.precheck_results["all_passed"] = True
        strategy.status = StrategyStatus.PENDING_APPROVAL
        print("⚠️  演示模式下自动调整前置检查为通过")

    flow = approval.generate_approval_flow(strategy)
    print(f"\n   审批流程ID: {flow.flow_id}")
    flow = approval.simulate_approval(flow, auto_approve=True)
    for step in flow.steps:
        print(f"   ✅ [{step.role.value}] {step.approver} - {step.status}")

    grayscale.init_grayscale(strategy)
    for _ in config.GRAYSCALE_ORDER:
        result = grayscale.advance_grayscale(strategy)
        for seg in result.get("advanced", []):
            ratio = strategy.grayscale_status.get(seg, {}).get("ratio", 0)
            print(f"   ✅ 灰度推送 [{seg}] {ratio*100:.0f}%")

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
    print(f"\n✅ 场景1完成: {strategy.name} {strategy.version} [{strategy.status.value}]")

    print(f"\n{'='*80}")
    print("场景2: 监控模拟3周期 (超阈值直接强制回滚，不询问)")
    print("=" * 80)
    for i in range(3):
        result = monitor.monitor_cycle(strategy)
        snap = result["snapshot"]
        violations = result["violations"]
        icon = "✅" if not violations else "⚠️ "
        print(
            f"   [{snap['timestamp']}] {icon} "
            f"通过率{snap['credit_approval_rate']*100:.1f}%  "
            f"欺诈{snap['fraud_detection_rate']*100:.1f}%  "
            f"延迟{snap['loan_delay_seconds']:.0f}s  "
            f"逾期{snap['overdue_anomaly_rate']*100:.2f}%"
        )
        if violations:
            for v in violations:
                print(f"   ⚠️  {v['metric']}: {v['value']}")

    print(f"\n{'='*80}")
    print("场景3: 风险强制回滚 - 验证版本和状态切换")
    print("=" * 80)
    sim_violations = [{"metric": "逾期异常", "value": 0.087, "threshold": {"max": 0.05}}]
    record = rollback_mod.trigger_rollback(strategy, sim_violations)
    print(rollback_mod.generate_rollback_report(record))
    rollback_mod.restore_previous_strategy(strategy)
    storage.save_strategy(strategy)
    print(f"\n   🔍 回滚后状态检查:")
    print(f"      策略版本: {strategy.version} (应显示稳定版本)")
    print(f"      策略状态: {strategy.status.value} (应显示生效中)")

    print(f"\n{'='*80}")
    print("场景4: 监管风控整改 - 人工逐步审批模式 (演示模拟)")
    print("=" * 80)
    strat2 = RiskStrategy(
        strategy_id=f"STR-{uuid.uuid4().hex[:8].upper()}",
        name="监管整改-授信额度管控",
        version="v4.0.0",
        risk_level=RiskLevel.REGULATORY,
        description="落实银保监会2026年3号文授信集中度要求",
        credit_product="经营性贷款",
        status=StrategyStatus.DRAFT,
        previous_stable_version="v3.9.0-stable",
    )
    storage.save_strategy(strat2)
    strat2.status = StrategyStatus.PENDING_PRECHECK
    precheck.run_precheck(strat2)
    strat2.precheck_results["all_passed"] = True
    strat2.status = StrategyStatus.PENDING_APPROVAL
    flow2 = approval.generate_approval_flow(strat2)
    print(f"   审批流程ID: {flow2.flow_id} (监管类=4步:风控总监+授信总监+法务总监+合规总监)")

    # 演示模拟人工审批，一步步通过
    for i in range(len(flow2.steps)):
        step = flow2.steps[flow2.current_step]
        print(f"   📝 [{step.role.value}] {step.approver} 审批通过 (模拟人工操作)")
        approval.step_approve(flow2, comment=f"符合监管要求，审批通过-模拟{i+1}", approved=True)
    print(f"   最终状态: {flow2.status}")

    print(f"\n{'='*80}")
    print("场景5: 审批台账 - 按审批人/角色/风险级别查看")
    print("=" * 80)
    ledger = approval.query_approval_ledger()
    print(f"   总审批记录: {len(ledger)} 条")
    todo = approval.list_pending_tasks()
    done = approval.list_completed_approvals()
    rejected = approval.list_rejected_approvals()
    print(f"   待办: {len(todo)} | 已通过: {len(done)} | 已驳回: {len(rejected)}")
    by_role = approval.query_approval_ledger(role="风控")
    print(f"   风控角色审批数: {len(by_role)}")
    by_approver = approval.query_approval_ledger(approver="赵刚")
    print(f"   审批人[赵刚]相关: {len(by_approver)}")

    print(f"\n{'='*80}")
    print("场景6: 周报 - 真正的PDF趋势图 + Excel报表")
    print("=" * 80)
    stats = report_mod.generate_weekly_report(0)
    print(f"   📕 PDF: {stats['pdf_path']}")
    print(f"   📗 Excel: {stats['excel_path']}")
    print(f"   发布成功率: {stats['publish_success_rate']*100:.2f}%")
    print(f"   回滚次数: {stats['rollback_count']}")
    print(f"   欺诈拦截率: {stats['avg_fraud_detection_rate']*100:.2f}%")
    print(f"   产品分布: {dict(list(stats['product_distribution'].items())[:3])}...")
    print(f"   客群对比: {list(stats['segment_risk'].keys())}")

    print(f"\n{'='*80}")
    print("场景7: 查询 + 合规打包导出 (发布+回滚+审批+审计 ZIP)")
    print("=" * 80)
    recs = query_mod.query_publish_records()
    print(f"   查询到 {len(recs)} 条发布记录")
    zip_path = query_mod.batch_export(recs, export_format="csv")
    print(f"   📦 合规打包ZIP: {zip_path}")
    import zipfile
    with zipfile.ZipFile(zip_path, "r") as zf:
        print(f"   ZIP内文件: {zf.namelist()}")

    print(f"\n{'='*80}")
    print("场景8: 回滚演练")
    print("=" * 80)
    strat3 = RiskStrategy(
        strategy_id=f"STR-{uuid.uuid4().hex[:8].upper()}",
        name="紧急欺诈拦截策略V2",
        version="v2.5.0",
        risk_level=RiskLevel.EMERGENCY_FRAUD,
        description="针对新型团伙欺诈的紧急拦截策略",
        credit_product="经营性贷款",
        status=StrategyStatus.ACTIVE,
        previous_stable_version="v2.4.0-stable",
    )
    storage.save_strategy(strat3)
    drill_rec = drill_mod.create_drill(strat3, operator="风控管理员")
    print(f"   演练ID: {drill_rec.drill_id}")
    print(f"   欺诈校验: {drill_rec.fraud_simulation_result[:70]}...")
    print(f"   处置结果: {drill_rec.risk_disposal_result[:70]}...")

    print(f"\n{'='*80}")
    print("场景9: 策略列表 - 验证回滚后版本/状态")
    print("=" * 80)
    all_s = storage.load_all_strategies()
    for s in all_s:
        print(f"   {s['strategy_id']}  {s['name']}  {s['version']}  [{s['status']}]")

    print(f"\n{'='*80}")
    print(f"✅ 演示流程全部完成！ 9大场景成功运行")
    print(f"{'='*80}")
    print("提示:")
    print("  运行 'python main.py daemon' 启动守护模式，自动监控+定时周报")
    print("  运行 'python main.py ledger --type todo' 查看待办审批")
    print("  运行 'python main.py report' 生成真正的PDF+Excel周报")
    print("  运行 'python main.py query --export' 合规打包导出")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(BANNER)
        print("使用方式 (--help 查看详细命令):")
        commands = [
            "demo            一键完整演示(9大场景)",
            "create          创建风控策略",
            "publish         发布策略(自动/人工审批二选一)",
            "monitor         实时风险监控(超阈值自动强制回滚)",
            "ledger          审批台账(待办/已办/驳回多维度筛选)",
            "daemon          守护值守模式(自动监控+定时周报+任务)",
            "submit          向守护进程提交发布任务",
            "manual-report   守护进程手动触发周报",
            "drill           回滚演练",
            "report          生成PDF趋势图+Excel报表周报",
            "query --export  合规打包ZIP导出(4类记录)",
            "audit           风险审计日志全程留痕查询",
            "list            策略列表(可验证回滚后版本/状态)",
        ]
        for cmd in commands:
            print(f"  python main.py {cmd}")
    else:
        main()
