"""
应用服务层：沙盒准入、评审回避、商业秘密授权、运行处置、
争议证据保全、停机恢复与主管视图。

约定：每个写操作各自在一个事务内完成；涉及多步状态变更的
操作（批准分配名额、退出释放名额、争议冻结）使用同一事务，
任一步失败则整体回滚。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from .clock import Clock
from .errors import (
    AccessDeniedError,
    CapacityError,
    ConflictError,
    ImmutableEvidenceError,
    StateError,
    ValidationError,
)
from .models import (
    Approval,
    ApprovalBoundary,
    Dispute,
    DisputeOutcome,
    DisputeStatus,
    EvidenceSnapshot,
    Material,
    MaterialKind,
    OutageRecord,
    Person,
    Project,
    ProjectStage,
    Review,
    ReviewDuty,
    Risk,
    RiskStatus,
    RunEvent,
    RunPath,
    RunStatus,
    KIND_DUTY,
)
from .store import RuleVersion, SecretGrant, Store, canonical


# 申请准入时必须齐备的十类材料（分阶段补齐即可，不要求一次交全）。
REQUIRED_KINDS: tuple[MaterialKind, ...] = (
    MaterialKind.SUBJECTS,
    MaterialKind.DATA_CATEGORIES,
    MaterialKind.PURPOSES,
    MaterialKind.DESTINATIONS,
    MaterialKind.LEGAL_BASES,
    MaterialKind.RISK_ASSESSMENT,
    MaterialKind.CONTRACT_COMMITMENTS,
    MaterialKind.TECHNICAL_CONTROLS,
    MaterialKind.TEST_METRICS,
    MaterialKind.RULES_ACK,
)

# 批准边界必须明确的监测指标维度（具体限值在批准时给出）。
REQUIRED_METRIC_KEYS: tuple[str, ...] = ("max_daily_users", "error_rate")


class SandboxService:
    def __init__(self, store: Store, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or Clock()

    def now(self) -> datetime:
        return self.clock.now()

    # ==================================================================
    # 一、规则版本：只约束尚未作出的决定
    # ==================================================================

    def publish_rule(
        self,
        title: str,
        content: dict[str, Any],
        effective_at: datetime | None = None,
        actor: str = "rule-admin",
    ) -> RuleVersion:
        moment = effective_at or self.now()
        next_version = self.store.latest_rule_version() + 1
        rule = RuleVersion(
            version=next_version,
            title=title,
            effective_at=moment,
            content=copy.deepcopy(content),
            fingerprint=canonical({"v": next_version, "c": content}),
        )
        with self.store:
            self.store.save_rule(rule)
            self.store.record(
                moment, actor, "rule.publish", f"rule-v{next_version}",
                {"title": title, "effective_at": moment.isoformat()},
            )
        return rule

    def current_rule_version(self) -> int:
        """当前时刻已生效、将用于新决定的规则版本。"""
        return self.store.latest_rule_version(self.now())

    # ==================================================================
    # 二、人员登记（职责、辅导关系、利益关系）
    # ==================================================================

    def register_person(
        self,
        person_id: str,
        name: str,
        duties: frozenset[ReviewDuty] = frozenset(),
        is_applicant: bool = False,
        dispute_officer: bool = False,
        actor: str = "registry",
    ) -> Person:
        person = Person(
            id=person_id,
            name=name,
            duties=frozenset(duties),
            is_applicant=is_applicant,
            dispute_officer=dispute_officer,
        )
        with self.store:
            self.store.save_person(person)
            self.store.record(self.now(), actor, "person.register", person_id, {"name": name})
        return person

    def mark_mentorship(self, person_id: str, project_id: str) -> None:
        """登记该人员曾参与该项目辅导：此后不得评审该项目的例外。"""
        with self.store:
            person = self.store.get_person(person_id)
            person.mentored_projects.add(project_id)

    def mark_interest(self, person_id: str, project_id: str) -> None:
        """登记该人员与申请方存在利益关系。"""
        with self.store:
            person = self.store.get_person(person_id)
            person.interested_projects.add(project_id)

    # ==================================================================
    # 三、项目与分阶段材料
    # ==================================================================

    def create_project(
        self, project_id: str, title: str, applicant_id: str, actor: str | None = None
    ) -> Project:
        moment = self.now()
        actor = actor or applicant_id
        with self.store:
            applicant = self.store.get_person(applicant_id)
            if not applicant.is_applicant:
                raise ValidationError("登记的申请方身份不符")
            if project_id in {p.id for p in self.store.list_projects()}:
                raise ValidationError("项目编号已存在")
            project = Project(
                id=project_id,
                title=title,
                applicant_id=applicant_id,
                created_at=moment,
            )
            self.store.save_project(project)
            self.store.record(moment, actor, "project.create", project_id, {"title": title})
        return project

    def submit_material(
        self,
        project_id: str,
        kind: MaterialKind,
        content: dict[str, Any],
        *,
        trade_secret: bool = False,
        submitted_by: str | None = None,
    ) -> Material:
        """分阶段补交或修订材料；争议冻结期间禁止改动冻结材料。"""
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            actor = submitted_by or project.applicant_id
            self._require_not_frozen(project)
            if project.stage in (ProjectStage.REJECTED, ProjectStage.EXITED):
                raise StateError("项目已驳回或退出，不能再补材料")
            if not isinstance(content, dict) or not content:
                raise ValidationError("材料内容不能为空")

            revisions = project.materials.setdefault(kind, [])
            material = Material(
                kind=kind,
                revision=len(revisions) + 1,
                content=copy.deepcopy(content),
                trade_secret=trade_secret,
                submitted_by=actor,
                submitted_at=moment,
            )
            revisions.append(material)
            # 准入决定钉住的是决定时的版本；决定后的修订只追加不改写，
            # 争议快照与批准记录仍可追溯到当时版本。
            action = "material.submit" if project.stage == ProjectStage.DRAFT else "material.revise"
            self.store.record(
                moment,
                actor,
                action,
                project_id,
                {"kind": kind.value, "revision": material.revision,
                 "trade_secret": trade_secret,
                 "stage": project.stage.value},
            )
        return material

    def material_progress(self, project_id: str) -> dict[MaterialKind, int]:
        """主管视图用：各类材料当前版本号（0 表示未提交）。"""
        project = self.store.get_project(project_id)
        return {kind: len(project.materials.get(kind, [])) for kind in MaterialKind}

    def read_material(
        self, project_id: str, kind: MaterialKind, reader_id: str
    ) -> dict[str, Any]:
        """读取最新版本材料；商业秘密仅向承担对应审查职责且获授权的人员开放。"""
        project = self.store.get_project(project_id)
        material = project.latest(kind)
        if material is None:
            raise KeyError(f"材料尚未提交: {kind.value}")
        reader = self.store.get_person(reader_id)

        if material.trade_secret:
            duty = KIND_DUTY[kind]
            if duty not in reader.duties:
                raise AccessDeniedError(f"该材料属于商业秘密，{reader.name}不承担{duty.value}审查职责")
            if not self.store.can_access_secret(project_id, reader_id, kind):
                raise AccessDeniedError("尚未取得该商业秘密材料的专项访问授权")

        with self.store:
            self.store.record(
                self.now(), reader_id, "material.read", project_id,
                {"kind": kind.value, "revision": material.revision,
                 "trade_secret": material.trade_secret},
            )
        return {
            "kind": kind.value,
            "revision": material.revision,
            "content": copy.deepcopy(material.content),
            "trade_secret": material.trade_secret,
            "submitted_at": material.submitted_at.isoformat(),
        }

    def grant_secret_access(
        self,
        project_id: str,
        person_id: str,
        kinds: frozenset[MaterialKind],
        granter_id: str,
        reason: str,
    ) -> SecretGrant:
        """按审查职责逐类开放商业秘密；不承担对应职责的申请直接拒绝。"""
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            person = self.store.get_person(person_id)
            granter = self.store.get_person(granter_id)
            if not granter.duties:
                raise AccessDeniedError("授权人须为承担审查管理职责的人员")
            overreach = {k for k in kinds if KIND_DUTY[k] not in person.duties}
            if overreach:
                raise AccessDeniedError(
                    "不得向不承担对应审查职责的人员开放商业秘密: "
                    + ",".join(sorted(k.value for k in overreach))
                )
            grant = SecretGrant(
                id=self.store.next_id("grant"),
                project_id=project_id,
                person_id=person_id,
                duty=next(iter({KIND_DUTY[k] for k in kinds})),
                kinds=frozenset(kinds),
                granted_by=granter_id,
                granted_at=moment,
                reason=reason,
            )
            self.store.add_grant(grant)
            self.store.record(
                moment, granter_id, "secret.grant", project_id,
                {"person": person_id, "kinds": sorted(k.value for k in kinds)},
            )
        return grant

    # ==================================================================
    # 四、评审：利益冲突强制回避
    # ==================================================================

    def assign_reviewer(
        self, project_id: str, person_id: str, scope: ReviewDuty
    ) -> None:
        """指派评审人；辅导过本项目或与申请方有利益关系的人员不得评审例外。"""
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            person = self.store.get_person(person_id)
            self._assert_no_conflict(person, project)
            if scope not in person.duties:
                raise ConflictError(f"{person.name}不承担{scope.value}审查职责，不能就此评审")
            project.assigned_reviewers[person_id] = scope
            self.store.record(
                moment, person_id, "review.assign", project_id, {"scope": scope.value}
            )

    @staticmethod
    def _assert_no_conflict(person: Person, project: Project) -> None:
        if person.id == project.applicant_id:
            raise ConflictError("申请方不得评审本项目的例外")
        if project.id in person.mentored_projects:
            raise ConflictError(f"{person.name}曾参与本项目辅导，依法应当回避")
        if project.id in person.interested_projects:
            raise ConflictError(f"{person.name}与申请方存在利益关系，依法应当回避")

    def submit_review(
        self,
        project_id: str,
        reviewer_id: str,
        approve: bool,
        note: str = "",
        requested_scope: ReviewDuty | None = None,
    ) -> Review:
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            reviewer = self.store.get_person(reviewer_id)
            self._assert_no_conflict(reviewer, project)
            scope = project.assigned_reviewers.get(reviewer_id)
            if scope is None:
                scope = requested_scope
                if scope is None or scope not in reviewer.duties:
                    raise ConflictError("该人员未被指派且未声明匹配的审查职责，不得评审")
            review = Review(
                id=self.store.next_id("review"),
                reviewer_id=reviewer_id,
                scope=scope,
                approve=approve,
                note=note,
                decided_at=moment,
            )
            project.reviews.append(review)
            self.store.record(
                moment, reviewer_id, "review.submit", project_id,
                {"scope": scope.value, "approve": approve},
            )
        return review

    # ==================================================================
    # 五、提交申请与作出准入批准（名额、规则版本、五项边界同事务）
    # ==================================================================

    def submit_application(self, project_id: str) -> None:
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            if project.stage != ProjectStage.DRAFT:
                raise StateError("只有补正阶段的项目可以提交评审")
            missing = [k.value for k in REQUIRED_KINDS if project.latest(k) is None]
            if missing:
                raise ValidationError("材料尚未齐备，不能申请试运行: " + ",".join(missing))
            project.stage = ProjectStage.PENDING_REVIEW
            self.store.record(moment, project.applicant_id, "application.submit", project_id, {})

    def decide_approval(
        self,
        project_id: str,
        boundary: ApprovalBoundary,
        *,
        actor: str = "approval-board",
    ) -> Approval:
        """
        作出准入批准。同一事务内：
        1. 校验评审结论齐备且无反对、申请处于待决；
        2. 钉住当前规则版本（此后规则更新不影响本决定）；
        3. 占用一个沙盒名额（名额不足则整笔失败）。
        原批准决定此后永久保留，不因缩限/整改/暂停/退出而修改。
        """
        moment = self.now()
        self._validate_boundary(boundary)
        with self.store:
            project = self.store.get_project(project_id)
            if project.stage != ProjectStage.PENDING_REVIEW:
                raise StateError("项目未处于待评审状态，不能作出准入决定")
            if not project.assigned_reviewers:
                raise ValidationError("尚未指派任何评审人员")
            if len(project.reviews) < len(project.assigned_reviewers):
                raise ValidationError("仍有评审意见未提交")
            opposing = [r.reviewer_id for r in project.reviews if not r.approve]
            if opposing:
                raise ValidationError("存在反对意见，不能批准试运行: " + ",".join(opposing))
            # 评审提交后被补登的利益关系同样拦截：决定一刻必须干净。
            for review in project.reviews:
                self._assert_no_conflict(self.store.get_person(review.reviewer_id), project)

            if self.store.free_slots() <= 0:
                raise CapacityError("沙盒容量已满，须待在运项目退出释放名额后再作决定")

            rule_version = self.store.latest_rule_version(moment)
            if rule_version < 1:
                raise ValidationError("尚未发布任何规则版本，无据以批准")
            ack = project.latest(MaterialKind.RULES_ACK)
            if ack is not None and ack.content.get("rule_version") not in (None, rule_version):
                raise ValidationError(
                    f"企业知悉的规则版本v{ack.content.get('rule_version')}"
                    f"与当前生效v{rule_version}不一致"
                )

            approval = Approval(
                id=self.store.next_id("approval"),
                boundary=boundary,
                rule_version=rule_version,
                decided_at=moment,
                decided_by=[r.reviewer_id for r in project.reviews],
                material_revisions={
                    kind.value: len(project.materials.get(kind, []))
                    for kind in REQUIRED_KINDS
                },
            )
            project.approval = approval
            project.stage = ProjectStage.APPROVED
            project.run_status = RunStatus.NOT_STARTED
            project.slot_held = True
            self.store.record(
                moment, actor, "approval.grant", project_id,
                {"approval_id": approval.id, "rule_version": rule_version,
                 "max_users": boundary.max_users,
                 "end_at": boundary.end_at.isoformat()},
            )
        return project.approval

    def reject_application(self, project_id: str, reason: str, actor: str = "approval-board") -> None:
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            if project.stage != ProjectStage.PENDING_REVIEW:
                raise StateError("项目未处于待评审状态，不能驳回")
            project.stage = ProjectStage.REJECTED
            self.store.record(moment, actor, "approval.reject", project_id, {"reason": reason})

    @staticmethod
    def _validate_boundary(boundary: ApprovalBoundary) -> None:
        if not boundary.data_scopes:
            raise ValidationError("批准必须明确允许的数据范围")
        if boundary.end_at <= boundary.start_at:
            raise ValidationError("试运行持续时间必须为正")
        if boundary.max_users <= 0:
            raise ValidationError("用户规模必须为正")
        if not boundary.monitoring_metrics:
            raise ValidationError("批准必须明确监测指标及限值")
        for key in REQUIRED_METRIC_KEYS:
            if key not in boundary.monitoring_metrics:
                raise ValidationError(f"监测指标缺少必备维度: {key}")
        if not boundary.exit_conditions:
            raise ValidationError("批准必须明确退出条件")

    def start_trial(self, project_id: str) -> None:
        with self.store:
            project = self.store.get_project(project_id)
            if project.stage != ProjectStage.APPROVED or project.approval is None:
                raise StateError("项目未获准试运行")
            if project.run_status != RunStatus.NOT_STARTED:
                raise StateError("试运行已经开始")
            project.run_status = RunStatus.NORMAL
            self.store.record(self.now(), project.applicant_id, "trial.start", project_id, {})

    # ==================================================================
    # 六、风险登记与解除（主管视图中的"未解除风险"）
    # ==================================================================

    def open_risk(self, project_id: str, description: str, opened_by: str) -> Risk:
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            risk = Risk(
                id=self.store.next_id("risk"),
                description=description,
                status=RiskStatus.OPEN,
                opened_at=moment,
            )
            project.risks.append(risk)
            self.store.record(moment, opened_by, "risk.open", project_id,
                              {"risk_id": risk.id, "description": description})
        return risk

    def relieve_risk(self, project_id: str, risk_id: str, note: str, actor: str) -> Risk:
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            risk = next((r for r in project.risks if r.id == risk_id), None)
            if risk is None:
                raise KeyError(f"风险不存在: {risk_id}")
            if risk.status == RiskStatus.RELIEVED:
                raise StateError("风险已解除")
            risk.status = RiskStatus.RELIEVED
            risk.relieved_at = moment
            risk.relief_note = note
            self.store.record(moment, actor, "risk.relieve", project_id,
                              {"risk_id": risk_id, "note": note})
        return risk

    # ==================================================================
    # 七、运行期间四条处置路径；原批准决定继续保留
    # ==================================================================

    def _running_project(self, project_id: str) -> Project:
        project = self.store.get_project(project_id)
        if project.stage != ProjectStage.APPROVED:
            raise StateError("项目未处于获准试运行阶段")
        if project.run_status == RunStatus.EXITED:
            raise StateError("项目已退出，不能再进入处置路径")
        return project

    def _append_event(
        self,
        project: Project,
        path: RunPath,
        reason: str,
        trigger: str,
        actor: str,
        deadline: datetime | None = None,
        curtail_scopes: list[str] | None = None,
        dispute_id: str | None = None,
    ) -> RunEvent:
        event = RunEvent(
            seq=len(project.events) + 1,
            path=path,
            reason=reason,
            trigger=trigger,
            at=self.now(),
            actor=actor,
            deadline=deadline,
            curtail_scopes=list(curtail_scopes) if curtail_scopes else None,
            dispute_id=dispute_id,
        )
        project.events.append(event)
        self.store.record(
            event.at, actor, f"run.{path.value}", project.id,
            {"event_seq": event.seq, "trigger": trigger,
             "deadline": deadline.isoformat() if deadline else None},
        )
        return event

    def curtail_for_purpose_expansion(
        self, project_id: str, new_purpose: str, allowed_scopes: list[str], actor: str
    ) -> RunEvent:
        """用途扩张：先缩限到明确列举的数据范围，扩张用途不在范围内。"""
        if not allowed_scopes:
            raise ValidationError("缩限必须保留至少一项明确允许的数据范围")
        with self.store:
            project = self._running_project(project_id)
            approved = {s for s in self.effective_boundary(project).data_scopes}
            if not set(allowed_scopes) <= approved:
                raise ValidationError("缩限范围只能是原批准数据范围的子集")
            project.run_status = RunStatus.CURTAILED
            event = self._append_event(
                project, RunPath.CURTAIL,
                reason=f"发现处理目的扩张: {new_purpose}",
                trigger="purpose_expansion", actor=actor,
                curtail_scopes=allowed_scopes,
            )
        return event

    def report_metric_reading(
        self, project_id: str, metric: str, value: float, reporter: str
    ) -> None:
        """登记监测指标实测值；超过批准限值即自动进入限期整改路径。"""
        moment = self.now()
        with self.store:
            project = self._running_project(project_id)
            project.readings.append(
                {"at": moment.isoformat(), "metric": metric, "value": value,
                 "reporter": reporter}
            )
            self.store.record(moment, reporter, "metric.report", project_id,
                              {"metric": metric, "value": value})
            limit = self.effective_boundary(project).monitoring_metrics.get(metric)
            if limit is not None and value > limit:
                deadline = self.clock.deadline_after(timedelta(days=15))
                project.run_status = RunStatus.RECTIFYING
                self._append_event(
                    project, RunPath.RECTIFY,
                    reason=f"监测指标{metric}实测{value}超过限值{limit}",
                    trigger="metric_exceeded", actor=reporter, deadline=deadline,
                )

    def security_incident(
        self, project_id: str, incident: str, actor: str
    ) -> RunEvent:
        """发生安全事件：立即暂停。"""
        with self.store:
            project = self._running_project(project_id)
            project.suspended_from = project.run_status
            project.run_status = RunStatus.SUSPENDED
            event = self._append_event(
                project, RunPath.SUSPEND, reason=incident,
                trigger="security_incident", actor=actor,
            )
        return event

    def partner_withdraws(self, project_id: str, partner: str, actor: str) -> RunEvent:
        """合作方退出：进入提前退出路径，并在同一事务释放沙盒名额。"""
        return self._exit(
            project_id,
            path=RunPath.EARLY_EXIT,
            reason=f"合作方退出: {partner}",
            trigger="partner_withdrawal",
            actor=actor,
        )

    def exit_on_condition(self, project_id: str, condition: str, actor: str) -> RunEvent:
        """退出条件成就（含试运行期满）：正常退出并释放名额。"""
        return self._exit(
            project_id,
            path=RunPath.EARLY_EXIT,
            reason=f"退出条件成就: {condition}",
            trigger="exit_condition",
            actor=actor,
        )

    def _exit(
        self, project_id: str, path: RunPath, reason: str, trigger: str, actor: str
    ) -> RunEvent:
        """
        退出生效与容量释放必须处于同一事务：
        先标记退出并释放名额、再追加事件与记录；任一步失败全部回滚，
        绝不会出现"旧项目仍在运行、名额已分给新申请"的窗口。
        """
        with self.store:
            project = self._running_project(project_id)
            event = self._append_event(
                project, path, reason=reason, trigger=trigger, actor=actor
            )
            project.run_status = RunStatus.EXITED
            project.stage = ProjectStage.EXITED
            project.slot_held = False
            project.approval.active = False  # 批准决定记录本身原样保留
            self.store.record(self.now(), actor, "slot.release", project_id,
                              {"event_seq": event.seq})
        return event

    # ---- 处置后的恢复/解除 --------------------------------------------

    def resume_from_rectification(
        self, project_id: str, note: str, actor: str
    ) -> RunEvent:
        """整改通过：恢复正常运行；超过整改期限未完成的整改不得解除。"""
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            if project.run_status != RunStatus.RECTIFYING:
                raise StateError("项目不处于整改状态")
            event = project.events[-1]
            if event.deadline is not None and moment > event.deadline:
                raise StateError(
                    "整改已超过期限，不能直接恢复；应转入暂停或提前退出路径"
                )
            event.restored_at = moment
            event.restored_note = note
            project.run_status = RunStatus.NORMAL
            self.store.record(moment, actor, "rectify.restore", project_id,
                              {"event_seq": event.seq})
        return event

    def lift_curtailment(self, project_id: str, note: str, actor: str) -> RunEvent:
        """缩限解除：确认扩张用途已剥离后恢复原边界。"""
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            if project.run_status != RunStatus.CURTAILED:
                raise StateError("项目不处于缩限状态")
            event = project.events[-1]
            event.restored_at = moment
            event.restored_note = note
            project.run_status = RunStatus.NORMAL
            self.store.record(moment, actor, "curtail.restore", project_id,
                              {"event_seq": event.seq})
        return event

    def resume_from_suspension(
        self, project_id: str, note: str, actor: str
    ) -> RunEvent:
        """暂停解除：回到暂停发生前的真实运行阶段。"""
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            if project.run_status != RunStatus.SUSPENDED:
                raise StateError("项目不处于暂停状态")
            event = project.events[-1]
            event.restored_at = moment
            event.restored_note = note
            project.run_status = project.suspended_from or RunStatus.NORMAL
            project.suspended_from = None
            self.store.record(moment, actor, "suspend.restore", project_id,
                              {"event_seq": event.seq,
                               "resume_to": project.run_status.value})
        return event

    # ==================================================================
    # 八、服务中断：跨期限顺延并保存证据
    # ==================================================================

    def service_outage_begin(self, reason: str, actor: str = "ops") -> datetime:
        # 时钟暂停先于事务生效：若记录失败，事务回滚而时钟状态由调用方
        # 通过再次 begin/end 配对纠正；暂停本身不应被业务异常"撤销"。
        moment = self.now()
        if self.clock.is_paused:
            raise StateError("服务已处于中断状态")
        self.clock.pause(moment)
        with self.store:
            self.store.record(moment, actor, "outage.begin", "platform", {"reason": reason})
        return moment

    def service_outage_end(
        self, project_ids: list[str], reason: str, actor: str = "ops"
    ) -> timedelta:
        """
        恢复服务。中断期间冻结的整改期限按中断时长顺延，
        恢复后项目仍按原计划推进；每条顺延都记录在案作为证据。
        时钟恢复在事务外完成，事务只负责把顺延结果与证据落库。
        """
        if not self.clock.is_paused:
            raise StateError("当前没有进行中的服务中断")
        paused_at = self.clock._paused_at  # noqa: SLF001 - 同包时钟协作
        downtime = self.clock.resume()
        restored_at = self.now()
        with self.store:
            self.store.record(restored_at, actor, "outage.end", "platform",
                              {"reason": reason, "downtime_seconds": downtime.total_seconds()})
            for project_id in project_ids:
                project = self.store.get_project(project_id)
                affected: list[str] = []
                for event in project.events:
                    if (
                        event.deadline is not None
                        and event.restored_at is None
                        and paused_at is not None
                        and event.deadline > paused_at
                    ):
                        event.deadline = event.deadline + downtime
                        affected.append(f"event-{event.seq}")
                record = OutageRecord(
                    started_at=paused_at or restored_at,
                    ended_at=restored_at,
                    reason=reason,
                    affected_deadlines=affected,
                )
                project.outages.append(record)
                self.store.record(restored_at, actor, "deadline.postpone", project_id,
                                  {"downtime_seconds": downtime.total_seconds(),
                                   "affected": affected})
        return downtime

    # ==================================================================
    # 九、争议：提出即固定证据；裁决结果决定恢复或调整
    # ==================================================================

    def raise_dispute(
        self, project_id: str, raised_by: str, reason: str
    ) -> Dispute:
        """
        争议一旦提出，同一事务内固定：
        - 当时的全部材料集合（版本、内容、密级）；
        - 当时的访问授权名单（含已撤销记录）；
        - 截至当时的完整操作记录及哈希链末端。
        固定后材料集合禁止改动，后续裁决结果再决定恢复或调整。
        """
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            if project.is_frozen():
                raise StateError("项目已有未决争议，不能重复提出")
            dispute_id = self.store.next_id("dispute")

            materials_copy: list[dict[str, Any]] = []
            for kind, revisions in project.materials.items():
                for material in revisions:
                    materials_copy.append({
                        "kind": kind.value,
                        "revision": material.revision,
                        "content": copy.deepcopy(material.content),
                        "trade_secret": material.trade_secret,
                        "submitted_by": material.submitted_by,
                        "submitted_at": material.submitted_at.isoformat(),
                        "sealed": material.sealed,
                    })

            access_list = []
            for grant in self.store.grants_for(project_id):
                access_list.append({
                    "grant_id": grant.id,
                    "person_id": grant.person_id,
                    "duty": grant.duty.value,
                    "kinds": sorted(k.value for k in grant.kinds),
                    "granted_by": grant.granted_by,
                    "granted_at": grant.granted_at.isoformat(),
                    "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
                    "active": grant.active,
                })

            log_entries = [
                {
                    "seq": e.seq,
                    "at": e.at.isoformat(),
                    "actor": e.actor,
                    "action": e.action,
                    "target": e.target,
                    "detail": copy.deepcopy(e.detail),
                    "prev_hash": e.prev_hash,
                    "entry_hash": e.entry_hash,
                }
                for e in self.store.log()
            ]

            snapshot = EvidenceSnapshot(
                dispute_id=dispute_id,
                captured_at=moment,
                materials=materials_copy,
                access_list=access_list,
                log_entries=log_entries,
                log_tail_hash=self.store.log_tail_hash(),
            )
            dispute = Dispute(
                id=dispute_id,
                project_id=project_id,
                raised_by=raised_by,
                raised_at=moment,
                reason=reason,
                snapshot=snapshot,
            )
            project.disputes.append(dispute)
            # 冻结项目全部现有材料版本：争议期间不得增删改。
            for revisions in project.materials.values():
                for material in revisions:
                    material.sealed = True
            self.store.record(moment, raised_by, "dispute.raise", project_id,
                              {"dispute_id": dispute_id,
                               "materials_sealed": len(materials_copy),
                               "log_entries": len(log_entries),
                               "log_tail_hash": snapshot.log_tail_hash})
        return dispute

    def read_evidence(
        self, project_id: str, dispute_id: str, reader_id: str
    ) -> EvidenceSnapshot:
        """读取保全证据：争议解决人员或经原授权承担审查职责的人员方可查阅。"""
        project = self.store.get_project(project_id)
        dispute = next((d for d in project.disputes if d.id == dispute_id), None)
        if dispute is None:
            raise KeyError(f"争议不存在: {dispute_id}")
        reader = self.store.get_person(reader_id)
        if reader.dispute_officer:
            return dispute.snapshot
        if any(
            g.person_id == reader_id for g in self.store.active_grants(project_id)
        ) or reader_id in {r.reviewer_id for r in project.reviews}:
            return dispute.snapshot
        raise AccessDeniedError("无权查阅争议保全证据")

    def resolve_dispute(
        self,
        project_id: str,
        dispute_id: str,
        status: DisputeStatus,
        outcome: DisputeOutcome,
        note: str,
        actor: str,
        restore_to: RunStatus | None = None,
    ) -> Dispute:
        """
        调解、仲裁或诉讼有结果后：
        - RESTORE：解除冻结并恢复到争议前（或指定）运行阶段；
        - ADJUST：解除冻结，随后须通过 apply_dispute_adjustment 落定新边界；
        - TERMINATE/UNCHANGED：解除冻结，维持退出/维持现状。
        原批准决定始终保留，可被追溯。
        """
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            dispute = next((d for d in project.disputes if d.id == dispute_id), None)
            if dispute is None:
                raise KeyError(f"争议不存在: {dispute_id}")
            if dispute.status == DisputeStatus.RESOLVED:
                raise StateError("争议已经裁决")

            # status 入参记载裁决渠道（调解/仲裁/诉讼），争议本身进入终态。
            channel = status
            dispute.status = DisputeStatus.RESOLVED
            dispute.outcome = outcome
            dispute.resolved_at = moment
            dispute.resolution_note = f"[{channel.value}] {note}"

            for revisions in project.materials.values():
                for material in revisions:
                    material.sealed = False

            if outcome == DisputeOutcome.RESTORE:
                if project.run_status == RunStatus.EXITED:
                    raise StateError("已退出项目不能凭裁决直接恢复，须重新申请名额")
                project.run_status = restore_to or RunStatus.NORMAL
            # ADJUST / TERMINATE / UNCHANGED：运行阶段不变，由后续流程处理。

            self.store.record(moment, actor, "dispute.resolve", project_id,
                              {"dispute_id": dispute_id, "channel": status.value,
                               "outcome": outcome.value,
                               "run_status": project.run_status.value})
        return dispute

    def apply_dispute_adjustment(
        self,
        project_id: str,
        dispute_id: str,
        new_boundary: ApprovalBoundary,
        actor: str,
        note: str,
    ) -> "BoundaryAmendment":
        """
        裁决要求调整时落定新边界：另存为修订记录，原批准决定
        及其规则版本原样保留；项目的有效边界改以最新修订为准。
        """
        self._validate_boundary(new_boundary)
        moment = self.now()
        with self.store:
            project = self.store.get_project(project_id)
            dispute = next((d for d in project.disputes if d.id == dispute_id), None)
            if dispute is None:
                raise KeyError(f"争议不存在: {dispute_id}")
            if dispute.status != DisputeStatus.RESOLVED:
                raise StateError("须待争议裁决后才能按裁决调整边界")
            if dispute.outcome != DisputeOutcome.ADJUST:
                raise StateError("只有裁决为调整的争议才能落定修订边界")
            from .models import BoundaryAmendment

            amendment = BoundaryAmendment(
                id=self.store.next_id("amend"),
                dispute_id=dispute_id,
                boundary=new_boundary,
                decided_at=moment,
                decided_by=actor,
                note=note,
            )
            project.amendments.append(amendment)
            self.store.record(moment, actor, "boundary.amend", project_id,
                              {"dispute_id": dispute_id, "amendment_id": amendment.id,
                               "max_users": new_boundary.max_users})
        return amendment

    @staticmethod
    def effective_boundary(project: Project) -> ApprovalBoundary | None:
        """当前有效边界：有裁决修订时以最新修订为准，否则为原批准边界。"""
        if project.approval is None:
            return None
        if project.amendments:
            return project.amendments[-1].boundary
        return project.approval.boundary

    # ==================================================================
    # 十、主管视图：打开项目即见获准边界、真实阶段、整改期限、未解除风险
    # ==================================================================

    def supervisor_view(self, project_id: str, viewer_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        viewer = self.store.get_person(viewer_id)
        if not viewer.duties and not viewer.dispute_officer:
            raise AccessDeniedError("仅主管或审查人员可查看主管视图")

        boundary = None
        effective = self.effective_boundary(project)
        if project.approval is not None and effective is not None:
            b = effective
            boundary = {
                "data_scopes": list(b.data_scopes),
                "start_at": b.start_at.isoformat(),
                "end_at": b.end_at.isoformat(),
                "max_users": b.max_users,
                "monitoring_metrics": dict(b.monitoring_metrics),
                "exit_conditions": list(b.exit_conditions),
                "rule_version": project.approval.rule_version,
                "approval_id": project.approval.id,
                "approval_active": project.approval.active,
                "amended_by": (
                    project.amendments[-1].id if project.amendments else None
                ),
            }

        latest_event = project.events[-1] if project.events else None
        rectify_deadline = None
        if project.run_status == RunStatus.RECTIFYING and latest_event is not None:
            rectify_deadline = (
                latest_event.deadline.isoformat() if latest_event.deadline else None
            )

        return {
            "project_id": project.id,
            "title": project.title,
            "stage": project.stage.value,
            "run_status": project.run_status.value,
            "approved_boundary": boundary,
            "material_progress": {
                k.value: len(project.materials.get(k, [])) for k in MaterialKind
            },
            "open_risks": [
                {"id": r.id, "description": r.description,
                 "opened_at": r.opened_at.isoformat() if r.opened_at else None}
                for r in project.open_risks()
            ],
            "rectification_deadline": rectify_deadline,
            "slot_held": project.slot_held,
            "active_dispute": next(
                (d.id for d in project.disputes if d.status != DisputeStatus.RESOLVED),
                None,
            ),
            "last_event": (
                {"seq": latest_event.seq, "path": latest_event.path.value,
                 "trigger": latest_event.trigger, "at": latest_event.at.isoformat()}
                if latest_event is not None else None
            ),
            "outages": [
                {"started_at": o.started_at.isoformat(),
                 "ended_at": o.ended_at.isoformat() if o.ended_at else None,
                 "affected_deadlines": list(o.affected_deadlines)}
                for o in project.outages
            ],
            "current_rule_version": self.current_rule_version(),
        }

    def _require_not_frozen(self, project: Project) -> None:
        if project.is_frozen():
            raise ImmutableEvidenceError(
                f"项目存在未决争议 {next(d.id for d in project.disputes if d.status != DisputeStatus.RESOLVED)}，"
                "材料集合已固定，禁止改动"
            )
