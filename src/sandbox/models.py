"""合规沙盒的领域模型：项目、材料、人员、批准边界、运行事件、争议证据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MaterialKind(str, Enum):
    """企业按项目分阶段提交的十类材料。"""

    SUBJECTS = "subjects"                 # 参与主体
    DATA_CATEGORIES = "data_categories"   # 数据类别与来源
    PURPOSES = "purposes"                 # 处理目的
    DESTINATIONS = "destinations"         # 目的地（含境外接收方）
    LEGAL_BASES = "legal_bases"           # 法律依据
    RISK_ASSESSMENT = "risk_assessment"   # 风险评估
    CONTRACT_COMMITMENTS = "contracts"    # 合同承诺（含境外合同）
    TECHNICAL_CONTROLS = "controls"       # 技术控制
    TEST_METRICS = "metrics"              # 测试指标
    RULES_ACK = "rules_ack"               # 对当前规则版本的知悉确认


class ReviewDuty(str, Enum):
    """审查职责类别；商业秘密只向承担对应职责的评审人员开放。"""

    PERSONAL_INFO = "personal_info"   # 个人信息保护审查
    GEO_INFO = "geo_info"             # 地理信息安全审查
    ALGORITHM = "algorithm"           # 算法说明审查
    CROSS_BORDER = "cross_border"     # 出境与境外合同审查
    PAYMENT = "payment"               # 数字支付审查
    SECURITY = "security"             # 安全技术审查


# 每类材料对应的审查职责；被标为商业秘密的材料按此表限定可见范围。
KIND_DUTY: dict[MaterialKind, ReviewDuty] = {
    MaterialKind.SUBJECTS: ReviewDuty.CROSS_BORDER,
    MaterialKind.DATA_CATEGORIES: ReviewDuty.PERSONAL_INFO,
    MaterialKind.PURPOSES: ReviewDuty.PERSONAL_INFO,
    MaterialKind.DESTINATIONS: ReviewDuty.CROSS_BORDER,
    MaterialKind.LEGAL_BASES: ReviewDuty.CROSS_BORDER,
    MaterialKind.RISK_ASSESSMENT: ReviewDuty.SECURITY,
    MaterialKind.CONTRACT_COMMITMENTS: ReviewDuty.CROSS_BORDER,
    MaterialKind.TECHNICAL_CONTROLS: ReviewDuty.SECURITY,
    MaterialKind.TEST_METRICS: ReviewDuty.ALGORITHM,
    MaterialKind.RULES_ACK: ReviewDuty.CROSS_BORDER,
}


class ProjectStage(str, Enum):
    """准入生命周期阶段。"""

    DRAFT = "draft"                 # 材料分阶段补正中
    PENDING_REVIEW = "pending"      # 已提交，等待/正在评审
    APPROVED = "approved"           # 已作出准入批准（决定永久保留）
    REJECTED = "rejected"           # 未获准试运行
    EXITED = "exited"               # 批准后退出（期满或提前），决定仍保留


class RunStatus(str, Enum):
    """真实运行阶段，独立于准入决定并随处置路径变化。"""

    NOT_STARTED = "not_started"
    NORMAL = "normal"               # 按批准边界正常试运行
    CURTAILED = "curtailed"         # 缩限运行
    RECTIFYING = "rectifying"       # 限期整改
    SUSPENDED = "suspended"         # 暂停
    EXITED = "exited"               # 已退出（提前退出/期满退出）


class RunPath(str, Enum):
    """运行期间四条处置路径。"""

    CURTAIL = "curtail"             # 用途扩张等 -> 缩限
    RECTIFY = "rectify"             # 指标超限等 -> 整改
    SUSPEND = "suspend"             # 安全事件等 -> 暂停
    EARLY_EXIT = "early_exit"       # 合作方退出等 -> 提前退出


class RiskStatus(str, Enum):
    OPEN = "open"
    RELIEVED = "relieved"           # 已解除


class DisputeStatus(str, Enum):
    OPEN = "open"                   # 已提出，证据已固定
    MEDIATING = "mediating"
    ARBITRATING = "arbitrating"
    LITIGATING = "litigating"
    RESOLVED = "resolved"           # 调解/仲裁/诉讼已有结果


class DisputeOutcome(str, Enum):
    RESTORE = "restore"             # 恢复原边界运行
    ADJUST = "adjust"               # 调整后继续
    TERMINATE = "terminate"         # 维持退出
    UNCHANGED = "unchanged"         # 争议不成立，维持现状


@dataclass
class Material:
    kind: MaterialKind
    revision: int
    content: dict[str, Any]
    trade_secret: bool
    submitted_by: str
    submitted_at: datetime
    sealed: bool = False             # 争议冻结后置为真，禁止再修订


@dataclass
class Person:
    id: str
    name: str
    duties: frozenset[ReviewDuty] = frozenset()
    is_applicant: bool = False
    dispute_officer: bool = False       # 争议解决人员，可读取完整保全证据
    mentored_projects: set[str] = field(default_factory=set)   # 参与过辅导
    interested_projects: set[str] = field(default_factory=set)  # 有利益关系

    def has_conflict(self, project_id: str) -> bool:
        return (
            project_id in self.mentored_projects
            or project_id in self.interested_projects
        )


@dataclass
class Review:
    """一次评审意见。存在辅导或利益关系的人员不得成为评审人。"""

    id: str
    reviewer_id: str
    scope: ReviewDuty
    approve: bool
    note: str
    decided_at: datetime


@dataclass
class ApprovalBoundary:
    """取得试运行资格时必须明确的五项边界。"""

    data_scopes: list[str]          # 允许的数据范围（类别/来源）
    start_at: datetime
    end_at: datetime                # 持续时间
    max_users: int                  # 用户规模
    monitoring_metrics: dict[str, float]  # 监测指标：指标名 -> 限值
    exit_conditions: list[str]      # 退出条件

    def duration_days(self) -> int:
        return max(1, (self.end_at.date() - self.start_at.date()).days)


@dataclass
class Approval:
    """准入批准决定；后续缩限/整改/暂停/退出都不修改或删除本决定。"""

    id: str
    boundary: ApprovalBoundary
    rule_version: int               # 钉住作出决定时的规则版本
    decided_at: datetime
    decided_by: list[str]           # 参与作出决定的评审人
    material_revisions: dict[str, int] = field(default_factory=dict)
    # 决定时刻各材料类别的版本号，用于证明"当初允许试运行的边界"
    active: bool = True             # 退出后为 False，但记录保留


@dataclass
class BoundaryAmendment:
    """争议裁决调整：另起一份调整后边界，原批准决定原样保留。"""

    id: str
    dispute_id: str
    boundary: ApprovalBoundary
    decided_at: datetime
    decided_by: str
    note: str


@dataclass
class Risk:
    id: str
    description: str
    status: RiskStatus = RiskStatus.OPEN
    opened_at: datetime | None = None
    relieved_at: datetime | None = None
    relief_note: str = ""


@dataclass
class RunEvent:
    """处置路径与恢复动作的追加记录，构成项目运行轨迹。"""

    seq: int
    path: RunPath
    reason: str
    trigger: str
    at: datetime
    actor: str
    deadline: datetime | None = None     # 整改期限
    restored_at: datetime | None = None  # 解除/恢复时刻
    restored_note: str = ""
    curtail_scopes: list[str] | None = None
    dispute_id: str | None = None


@dataclass
class OutageRecord:
    """服务中断证据：中断跨越的期限在恢复后顺延。"""

    started_at: datetime
    ended_at: datetime | None
    reason: str
    affected_deadlines: list[str] = field(default_factory=list)


@dataclass
class EvidenceSnapshot:
    """争议提出时固定的材料集合、访问名单与操作记录。"""

    dispute_id: str
    captured_at: datetime
    materials: list[dict[str, Any]]      # 深拷贝：类别、版本、内容摘要/全文、密级
    access_list: list[dict[str, Any]]    # 深拷贝：当时的访问授权名单
    log_entries: list[dict[str, Any]]    # 深拷贝：截至当时的操作记录
    log_tail_hash: str                   # 操作链末端哈希，证明截取位置


@dataclass
class Dispute:
    id: str
    project_id: str
    raised_by: str
    raised_at: datetime
    reason: str
    snapshot: EvidenceSnapshot
    status: DisputeStatus = DisputeStatus.OPEN
    outcome: DisputeOutcome | None = None
    resolved_at: datetime | None = None
    resolution_note: str = ""


@dataclass
class Project:
    id: str
    title: str
    applicant_id: str
    created_at: datetime
    stage: ProjectStage = ProjectStage.DRAFT
    run_status: RunStatus = RunStatus.NOT_STARTED
    materials: dict[MaterialKind, list[Material]] = field(default_factory=dict)
    assigned_reviewers: dict[str, ReviewDuty] = field(default_factory=dict)  # person_id -> scope
    reviews: list[Review] = field(default_factory=list)
    approval: Approval | None = None
    amendments: list[BoundaryAmendment] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
    events: list[RunEvent] = field(default_factory=list)
    disputes: list[Dispute] = field(default_factory=list)
    outages: list[OutageRecord] = field(default_factory=list)
    readings: list[dict[str, Any]] = field(default_factory=list)  # 监测指标实测值
    slot_held: bool = False              # 是否占用沙盒容量名额
    suspended_from: RunStatus | None = None  # 暂停前的运行阶段，用于恢复

    def latest(self, kind: MaterialKind) -> Material | None:
        revisions = self.materials.get(kind)
        return revisions[-1] if revisions else None

    def is_frozen(self) -> bool:
        return any(d.status != DisputeStatus.RESOLVED for d in self.disputes)

    def open_risks(self) -> list[Risk]:
        return [r for r in self.risks if r.status == RiskStatus.OPEN]
