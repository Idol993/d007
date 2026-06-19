import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIT_LOG_DIR = os.path.join(DATA_DIR, "audit_logs")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
TASK_QUEUE_DIR = os.path.join(DATA_DIR, "tasks")
TASK_PENDING_DIR = os.path.join(TASK_QUEUE_DIR, "pending")
TASK_DONE_DIR = os.path.join(TASK_QUEUE_DIR, "done")

PRECHECK_THRESHOLDS = {
    "风控模型准确率": {"min": 0.85, "unit": "比率"},
    "反欺诈校验通过率": {"min": 0.90, "unit": "比率"},
    "征信数据合规性": {"min": 0.95, "unit": "比率"},
    "授信额度校验": {"min": 0.88, "unit": "比率"},
}

MONITOR_THRESHOLDS = {
    "授信通过率": {"min": 0.60, "max": 0.95, "direction": "range"},
    "欺诈识别率": {"min": 0.80, "direction": "min"},
    "放款延迟": {"max": 300, "direction": "max", "unit": "秒"},
    "逾期异常": {"max": 0.05, "direction": "max"},
}

MONITOR_INTERVAL_SECONDS = 60

APPROVAL_FLOW_RULES = {
    "常规策略迭代": [
        {"role": "风控", "approver": "风控主管-张伟"},
        {"role": "授信", "approver": "授信主管-李明"},
        {"role": "合规", "approver": "合规专员-王芳"},
    ],
    "紧急欺诈拦截": [
        {"role": "风控", "approver": "风控总监-赵刚"},
        {"role": "授信", "approver": "授信主管-李明"},
        {"role": "法务", "approver": "法务顾问-陈静"},
        {"role": "合规", "approver": "合规总监-刘洋"},
    ],
    "监管风控整改": [
        {"role": "风控", "approver": "风控总监-赵刚"},
        {"role": "授信", "approver": "授信总监-周磊"},
        {"role": "法务", "approver": "法务总监-黄丽"},
        {"role": "合规", "approver": "合规总监-刘洋"},
    ],
}

GRAYSCALE_ORDER = ["优质客群", "普通客群", "高风险客群"]
GRAYSCALE_RATIOS = {"优质客群": 0.10, "普通客群": 0.30, "高风险客群": 1.00}

ROLLBACK_NOTIFY_ROLES = ["风控", "授信", "贷后", "合规"]

CREDIT_PRODUCTS = ["个人消费贷", "经营性贷款", "住房按揭贷", "汽车金融贷", "信用卡分期"]

STRATEGY_DB_FILE = os.path.join(DATA_DIR, "strategies.json")
PUBLISH_RECORDS_FILE = os.path.join(DATA_DIR, "publish_records.json")
ROLLBACK_RECORDS_FILE = os.path.join(DATA_DIR, "rollback_records.json")
DRILL_RECORDS_FILE = os.path.join(DATA_DIR, "drill_records.json")
MONITOR_SNAPSHOTS_FILE = os.path.join(DATA_DIR, "monitor_snapshots.json")
APPROVAL_FLOWS_FILE = os.path.join(DATA_DIR, "approval_flows.json")
