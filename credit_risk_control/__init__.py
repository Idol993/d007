from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict


class RiskLevel(Enum):
    ROUTINE = "常规策略迭代"
    EMERGENCY_FRAUD = "紧急欺诈拦截"
    REGULATORY = "监管风控整改"


class CustomerSegment(Enum):
    PREMIUM = "优质客群"
    NORMAL = "普通客群"
    HIGH_RISK = "高风险客群"


class ApprovalRole(Enum):
    RISK_CONTROL = "风控"
    CREDIT = "授信"
    LEGAL = "法务"
    COMPLIANCE = "合规"


class StrategyStatus(Enum):
    DRAFT = "草稿"
    PENDING_PRECHECK = "待前置检查"
    PRECHECK_FAILED = "前置检查失败"
    PENDING_APPROVAL = "待审批"
    APPROVAL_REJECTED = "审批驳回"
    GRAYSCALE_DEPLOYING = "灰度发布中"
    FULL_DEPLOYED = "全量发布"
    ROLLING_BACK = "回滚中"
    ROLLED_BACK = "已回滚"
    ACTIVE = "生效中"


class MonitorMetric(Enum):
    CREDIT_APPROVAL_RATE = "授信通过率"
    FRAUD_DETECTION_RATE = "欺诈识别率"
    LOAN_DELAY = "放款延迟"
    OVERDUE_ANOMALY = "逾期异常"


@dataclass
class RiskStrategy:
    strategy_id: str
    name: str
    version: str
    risk_level: RiskLevel
    description: str
    credit_product: str
    status: StrategyStatus = StrategyStatus.DRAFT
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    precheck_results: Dict = field(default_factory=dict)
    approval_flow: Optional['ApprovalFlow'] = None
    grayscale_status: Dict = field(default_factory=dict)
    monitoring_data: Dict = field(default_factory=dict)
    previous_stable_version: Optional[str] = None


@dataclass
class ApprovalStep:
    step_order: int
    role: ApprovalRole
    approver: str
    status: str = "待审批"
    comment: str = ""
    approved_at: Optional[str] = None


@dataclass
class ApprovalFlow:
    flow_id: str
    strategy_id: str
    risk_level: RiskLevel
    steps: List[ApprovalStep] = field(default_factory=list)
    current_step: int = 0
    status: str = "待审批"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@dataclass
class PrecheckResult:
    check_name: str
    passed: bool
    value: float
    threshold: float
    detail: str = ""


@dataclass
class MonitorSnapshot:
    timestamp: str
    credit_approval_rate: float
    fraud_detection_rate: float
    loan_delay_seconds: float
    overdue_anomaly_rate: float
    strategy_id: str = ""


@dataclass
class RollbackRecord:
    rollback_id: str
    strategy_id: str
    strategy_name: str
    strategy_version: str
    reason: str
    trigger_metric: str
    trigger_value: float
    threshold_value: float
    affected_segments: List[CustomerSegment]
    compliance_risk_desc: str
    previous_stable_version: str
    status: str = "已回滚"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    notified_roles: List[str] = field(default_factory=list)


@dataclass
class DrillRecord:
    drill_id: str
    strategy_id: str
    plan: str
    fraud_simulation_result: str
    risk_disposal_result: str
    status: str = "已完成"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@dataclass
class PublishRecord:
    record_id: str
    strategy_id: str
    strategy_name: str
    version: str
    risk_level: RiskLevel
    credit_product: str
    customer_segment: CustomerSegment
    status: StrategyStatus
    publish_time: str
    operator: str = ""
    rollback_count: int = 0


@dataclass
class AuditLogEntry:
    log_id: str
    action: str
    operator: str
    target_type: str
    target_id: str
    detail: str
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
