"""合规沙盒后端的端到端业务规则测试（全部使用虚构示例数据）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.sandbox import (
    AccessDeniedError,
    ApprovalBoundary,
    CapacityError,
    Clock,
    ConflictError,
    DisputeOutcome,
    DisputeStatus,
    ImmutableEvidenceError,
    MaterialKind as K,
    ReviewDuty as D,
    RunStatus,
    SandboxError,
    StateError,
    Store,
    ValidationError,
)
from src.sandbox.services import SandboxService

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


class ManualTime:
    """可手动推进的墙钟来源，配合 Clock 模拟服务中断与期限经过。"""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, days: int = 0, hours: int = 0) -> None:
        self.t += timedelta(days=days, hours=hours)


def make_boundary(start: datetime = T0) -> ApprovalBoundary:
    return ApprovalBoundary(
        data_scopes=["scope:脱敏个人信息", "scope:示例地理信息", "scope:风控特征"],
        start_at=start,
        end_at=start + timedelta(days=90),
        max_users=1000,
        monitoring_metrics={"max_daily_users": 1000.0, "error_rate": 0.01},
        exit_conditions=["试运行期满", "发生重大安全事件", "合作方退出"],
    )


MATERIAL_CONTENT = {
    K.SUBJECTS: {"applicant": "某数字贸易企业（虚构示例）"},
    K.DATA_CATEGORIES: {"categories": ["脱敏个人信息", "示例地理信息"]},
    K.PURPOSES: {"purposes": ["跨境风控模型试运行"]},
    K.DESTINATIONS: {"regions": ["示例地区A"], "receivers": ["示例境外接收方"]},
    K.LEGAL_BASES: {"bases": ["合同必需", "单独同意（示例）"]},
    K.RISK_ASSESSMENT: {"risks": ["出境泄露", "模型误判"], "level": "中"},
    K.CONTRACT_COMMITMENTS: {"commitments": ["本地化备份", "删除回执"]},
    K.TECHNICAL_CONTROLS: {"controls": ["加密传输", "访问审计"]},
    K.TEST_METRICS: {"metrics": ["日活用户", "差错率"]},
    K.RULES_ACK: {"rule_version": 1},
}

# 标为商业秘密的材料类别（分别对应个人信息、出境合同、算法审查职责）。
SECRET_KINDS = {K.DATA_CATEGORIES, K.CONTRACT_COMMITMENTS, K.TEST_METRICS}


class SandboxWorld:
    """搭建一组标准人员、规则与服务，供各测试在其上编排。"""

    def __init__(self, capacity: int = 10) -> None:
        self.manual = ManualTime(T0)
        self.clock = Clock(_source=self.manual)
        self.store = Store(capacity=capacity)
        self.svc = SandboxService(self.store, self.clock)

        self.svc.register_person("app", "示例企业经办人", is_applicant=True)
        self.svc.register_person("cb", "出境审查员陈某", duties=frozenset({D.CROSS_BORDER}))
        self.svc.register_person("pi", "个信审查员林某", duties=frozenset({D.PERSONAL_INFO}))
        self.svc.register_person("geo", "地理信息审查员赵某", duties=frozenset({D.GEO_INFO}))
        self.svc.register_person("algo", "算法审查员周某", duties=frozenset({D.ALGORITHM}))
        self.svc.register_person("sec", "安全审查员钱某", duties=frozenset({D.SECURITY}))
        self.svc.register_person("officer", "争议解决人员吴某", dispute_officer=True)
        self.svc.register_person("outsider", "无关人员郑某")
        self.svc.register_person("mentor", "曾参与辅导的孙某",
                                 duties=frozenset({D.CROSS_BORDER}))
        self.svc.publish_rule("数据跨境沙盒准入规则（虚构）v1",
                              {"limits": {"users": 1000}}, actor="admin")

    def new_project(self, pid: str = "P1", ack_version: int = 1) -> str:
        self.svc.create_project(pid, f"示例项目{pid}", "app")
        return pid

    def submit_all_materials(self, pid: str = "P1", ack_version: int = 1) -> None:
        for kind, content in MATERIAL_CONTENT.items():
            payload = dict(content)
            if kind is K.RULES_ACK:
                payload = {"rule_version": ack_version}
            self.svc.submit_material(
                pid, kind, payload, trade_secret=kind in SECRET_KINDS
            )

    def approve(
        self,
        pid: str = "P1",
        reviewer: str = "cb",
        boundary: ApprovalBoundary | None = None,
        ack_version: int = 1,
    ):
        self.submit_all_materials(pid, ack_version=ack_version)
        self.svc.submit_application(pid)
        self.svc.assign_reviewer(pid, reviewer, D.CROSS_BORDER)
        self.svc.submit_review(pid, reviewer, True, note="材料齐备，同意准入")
        return self.svc.decide_approval(pid, boundary or make_boundary())


class StagedMaterialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SandboxWorld()

    def test_materials_can_be_completed_in_stages_but_required_for_application(self) -> None:
        w = self.w
        w.new_project()
        # 先只交两类材料：项目留在补正阶段。
        w.svc.submit_material("P1", K.SUBJECTS, MATERIAL_CONTENT[K.SUBJECTS])
        w.svc.submit_material("P1", K.PURPOSES, MATERIAL_CONTENT[K.PURPOSES])
        with self.assertRaisesRegex(ValidationError, "材料尚未齐备"):
            w.svc.submit_application("P1")

        progress = w.svc.material_progress("P1")
        self.assertEqual(progress[K.SUBJECTS], 1)
        self.assertEqual(progress[K.RISK_ASSESSMENT], 0)

        for kind, content in MATERIAL_CONTENT.items():
            if progress[kind]:
                continue
            w.svc.submit_material("P1", kind, content,
                                  trade_secret=kind in SECRET_KINDS)
        w.svc.submit_application("P1")
        view = w.svc.supervisor_view("P1", "cb")
        self.assertEqual(view["stage"], "pending")

    def test_material_revisions_accumulate_before_decision(self) -> None:
        w = self.w
        w.new_project()
        w.svc.submit_material("P1", K.PURPOSES, {"purposes": ["初版用途"]})
        w.svc.submit_material("P1", K.PURPOSES, {"purposes": ["修订后用途"]})
        self.assertEqual(w.svc.material_progress("P1")[K.PURPOSES], 2)
        self.assertEqual(
            w.svc.read_material("P1", K.PURPOSES, "app")["content"],
            {"purposes": ["修订后用途"]},
        )


class ReviewConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SandboxWorld()
        self.w.new_project()
        self.w.submit_all_materials()
        self.w.svc.submit_application("P1")

    def test_mentor_and_interested_person_must_be_recused(self) -> None:
        w = self.w
        w.svc.mark_mentorship("mentor", "P1")
        with self.assertRaisesRegex(ConflictError, "辅导"):
            w.svc.assign_reviewer("P1", "mentor", D.CROSS_BORDER)

        w.svc.mark_interest("cb", "P1")
        with self.assertRaisesRegex(ConflictError, "利益关系"):
            w.svc.assign_reviewer("P1", "cb", D.CROSS_BORDER)

        with self.assertRaisesRegex(ConflictError, "申请方"):
            w.svc.assign_reviewer("P1", "app", D.CROSS_BORDER)

    def test_scope_mismatch_blocks_review(self) -> None:
        # 算法审查员不能承担出境职责的评审。
        with self.assertRaises(ConflictError):
            self.w.svc.assign_reviewer("P1", "algo", D.CROSS_BORDER)

    def test_application_with_opposing_review_cannot_be_approved(self) -> None:
        w = self.w
        w.svc.assign_reviewer("P1", "cb", D.CROSS_BORDER)
        w.svc.submit_review("P1", "cb", False, note="境外合同承诺不足")
        with self.assertRaisesRegex(ValidationError, "反对意见"):
            w.svc.decide_approval("P1", make_boundary())
        self.assertEqual(w.store.free_slots(), 10)  # 未占用名额


class TradeSecretTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SandboxWorld()
        self.w.new_project()
        self.w.submit_all_materials()

    def test_secret_only_open_to_duty_bound_granted_reviewer(self) -> None:
        w = self.w
        # 无关人员、不承担个信职责的算法审查员均不可读。
        with self.assertRaises(AccessDeniedError):
            w.svc.read_material("P1", K.DATA_CATEGORIES, "outsider")
        with self.assertRaisesRegex(AccessDeniedError, "审查职责"):
            w.svc.read_material("P1", K.DATA_CATEGORIES, "algo")
        # 承担职责但未获专项授权，同样不可读。
        with self.assertRaisesRegex(AccessDeniedError, "专项访问授权"):
            w.svc.read_material("P1", K.DATA_CATEGORIES, "pi")

        # 不得向不承担对应职责的人员授权。
        with self.assertRaises(AccessDeniedError):
            w.svc.grant_secret_access("P1", "algo", frozenset({K.DATA_CATEGORIES}),
                                      granter_id="sec", reason="越权测试")

        w.svc.grant_secret_access("P1", "pi", frozenset({K.DATA_CATEGORIES}),
                                  granter_id="sec", reason="履行个信审查职责")
        viewed = w.svc.read_material("P1", K.DATA_CATEGORIES, "pi")
        self.assertEqual(viewed["revision"], 1)
        self.assertTrue(viewed["trade_secret"])

    def test_non_secret_material_remains_visible_to_review_duty_staff(self) -> None:
        # 未标密的材料（如处理目的）不设密级门槛。
        self.w.svc.submit_material("P1", K.SUBJECTS, MATERIAL_CONTENT[K.SUBJECTS])
        self.w.svc.read_material("P1", K.SUBJECTS, "cb")


class ApprovalBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SandboxWorld()

    def test_approval_requires_explicit_boundary(self) -> None:
        w = self.w
        w.new_project()
        w.submit_all_materials()
        w.svc.submit_application("P1")
        w.svc.assign_reviewer("P1", "cb", D.CROSS_BORDER)
        w.svc.submit_review("P1", "cb", True)

        bad = ApprovalBoundary(
            data_scopes=[], start_at=T0, end_at=T0 + timedelta(days=30),
            max_users=10, monitoring_metrics={"max_daily_users": 10.0},
            exit_conditions=["期满"],
        )
        with self.assertRaisesRegex(ValidationError, "数据范围"):
            w.svc.decide_approval("P1", bad)
        with self.assertRaisesRegex(ValidationError, "监测指标"):
            w.svc.decide_approval("P1", ApprovalBoundary(
                data_scopes=["s1"], start_at=T0, end_at=T0 + timedelta(days=30),
                max_users=10, monitoring_metrics={"max_daily_users": 10.0},
                exit_conditions=["期满"]))

    def test_approval_pins_rule_version_boundary_and_consumes_slot(self) -> None:
        w = self.w
        w.new_project("P1")
        approval = w.approve("P1")
        self.assertEqual(approval.rule_version, 1)
        self.assertEqual(approval.active, True)
        project = w.store.get_project("P1")
        self.assertTrue(project.slot_held)
        self.assertEqual(w.store.free_slots(), 9)
        self.assertEqual(project.run_status, RunStatus.NOT_STARTED)

        w.svc.start_trial("P1")
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.NORMAL)

        # 批准后可以追加修订，但批准决定钉住的仍是决定时的第 1 版。
        w.svc.submit_material("P1", K.PURPOSES, {"purposes": ["事后补充用途"]})
        project = w.store.get_project("P1")
        self.assertEqual(project.approval.material_revisions["purposes"], 1)
        self.assertEqual(project.latest(K.PURPOSES).revision, 2)
        view = w.svc.supervisor_view("P1", "cb")
        self.assertEqual(view["approved_boundary"]["approval_id"], approval.id)


class RuleVersionIsolationTest(unittest.TestCase):
    def test_rule_update_only_affects_decisions_not_yet_made(self) -> None:
        w = SandboxWorld()
        w.new_project("P1")
        approval_a = w.approve("P1")
        self.assertEqual(approval_a.rule_version, 1)

        w.manual.advance(days=10)
        w.svc.publish_rule("准入规则v2（虚构）", {"limits": {"users": 500}}, actor="admin")

        # 旧项目仍按 v1 的边界运行，主管视图显示其钉住的版本。
        view_a = w.svc.supervisor_view("P1", "cb")
        self.assertEqual(view_a["approved_boundary"]["rule_version"], 1)
        self.assertEqual(view_a["current_rule_version"], 2)
        self.assertEqual(view_a["approved_boundary"]["max_users"], 1000)

        # 新项目按 v2 决定；其规则知悉确认也须为 v2。
        w.new_project("P2", ack_version=2)
        approval_b = w.approve(
            "P2",
            boundary=ApprovalBoundary(
                data_scopes=["scope:示例地理信息"],
                start_at=w.clock.now(), end_at=w.clock.now() + timedelta(days=30),
                max_users=500,
                monitoring_metrics={"max_daily_users": 500.0, "error_rate": 0.005},
                exit_conditions=["期满"],
            ),
            ack_version=2,
        )
        self.assertEqual(approval_b.rule_version, 2)


class RunPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SandboxWorld()
        self.w.new_project()
        self.w.approve()
        self.w.svc.start_trial("P1")

    def test_purpose_expansion_triggers_curtailment_and_can_be_lifted(self) -> None:
        w = self.w
        event = w.svc.curtail_for_purpose_expansion(
            "P1", "未批准的精准营销", ["scope:风控特征"], "sec"
        )
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.CURTAILED)
        self.assertEqual(event.curtail_scopes, ["scope:风控特征"])
        # 缩限只能在原批准范围内。
        with self.assertRaises(ValidationError):
            w.svc.curtail_for_purpose_expansion(
                "P1", "再次扩张", ["scope:未批准范围"], "sec"
            )
        w.svc.lift_curtailment("P1", "扩张用途已剥离", "sec")
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.NORMAL)
        # 原批准决定原样保留。
        self.assertTrue(w.store.get_project("P1").approval.active)

    def test_metric_exceedance_triggers_rectification_with_deadline(self) -> None:
        w = self.w
        w.svc.report_metric_reading("P1", "error_rate", 0.002, reporter="app")
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.NORMAL)

        w.svc.report_metric_reading("P1", "error_rate", 0.05, reporter="app")
        project = w.store.get_project("P1")
        self.assertEqual(project.run_status, RunStatus.RECTIFYING)
        deadline = project.events[-1].deadline
        self.assertEqual(deadline, T0 + timedelta(days=15))

        view = w.svc.supervisor_view("P1", "cb")
        self.assertEqual(view["rectification_deadline"], deadline.isoformat())

        w.svc.resume_from_rectification("P1", "模型已回滚，指标回落", "sec")
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.NORMAL)

    def test_overdue_rectification_cannot_be_directly_restored(self) -> None:
        w = self.w
        w.svc.report_metric_reading("P1", "error_rate", 0.9, reporter="app")
        w.manual.advance(days=16)
        with self.assertRaisesRegex(StateError, "超过期限"):
            w.svc.resume_from_rectification("P1", "超期整改", "sec")

    def test_security_incident_suspends_and_resumes_to_prior_phase(self) -> None:
        w = self.w
        w.svc.curtail_for_purpose_expansion("P1", "扩张用途", ["scope:风控特征"], "sec")
        w.svc.security_incident("P1", "发现异常访问（虚构）", "sec")
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.SUSPENDED)
        w.svc.resume_from_suspension("P1", "风险已阻断", "sec")
        # 回到暂停前所处的"缩限"阶段，而不是笼统地恢复正常。
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.CURTAILED)

    def test_partner_withdrawal_exits_releases_slot_and_keeps_decision(self) -> None:
        w = self.w
        before = w.store.held_slots()
        event = w.svc.partner_withdraws("P1", "示例境外接收方", "cb")
        project = w.store.get_project("P1")
        self.assertEqual(project.run_status, RunStatus.EXITED)
        self.assertEqual(project.stage.value, "exited")
        self.assertFalse(project.slot_held)
        self.assertEqual(w.store.held_slots(), before - 1)
        # 原批准决定继续保留、可追溯。
        self.assertIsNotNone(project.approval)
        self.assertFalse(project.approval.active)
        self.assertEqual(project.approval.rule_version, 1)
        self.assertEqual(project.events[-1].seq, event.seq)


class CapacityAtomicityTest(unittest.TestCase):
    def test_capacity_release_and_exit_are_one_transaction(self) -> None:
        w = SandboxWorld(capacity=1)
        w.new_project("P1")
        w.approve("P1")
        w.svc.start_trial("P1")
        self.assertEqual(w.store.free_slots(), 0)

        # 旧项目仍在运行时，新申请无法获得名额。
        w.new_project("P2")
        w.submit_all_materials("P2")
        w.svc.submit_application("P2")
        w.svc.assign_reviewer("P2", "cb", D.CROSS_BORDER)
        w.svc.submit_review("P2", "cb", True)
        with self.assertRaises(CapacityError):
            w.svc.decide_approval("P2", make_boundary())
        self.assertEqual(w.store.get_project("P2").stage.value, "pending")
        self.assertFalse(w.store.get_project("P2").slot_held)

        # 退出与释放名额同事务：退出成功后名额立即可给新项目。
        w.svc.partner_withdraws("P1", "合作方退出（虚构）", "cb")
        self.assertEqual(w.store.free_slots(), 1)
        w.svc.decide_approval("P2", make_boundary(start=w.clock.now()))
        self.assertEqual(w.store.free_slots(), 0)

    def test_failure_during_exit_rolls_back_slot_and_state(self) -> None:
        w = SandboxWorld(capacity=1)
        w.new_project("P1")
        w.approve("P1")
        w.svc.start_trial("P1")

        original_record = w.store.record

        def sabotage(at, actor, action, target, detail=None):
            if action == "slot.release":
                raise RuntimeError("模拟释放步骤失败")
            return original_record(at, actor, action, target, detail)

        w.store.record = sabotage
        try:
            with self.assertRaisesRegex(RuntimeError, "模拟释放步骤失败"):
                w.svc.partner_withdraws("P1", "合作方退出（虚构）", "cb")
        finally:
            w.store.record = original_record

        project = w.store.get_project("P1")
        # 整笔回滚：项目仍在运行、名额未释放、处置事件未留下半截记录。
        self.assertEqual(project.run_status, RunStatus.NORMAL)
        self.assertTrue(project.slot_held)
        self.assertEqual(w.store.free_slots(), 0)
        self.assertTrue(w.store.verify_log_chain())


class DisputeEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SandboxWorld()
        self.w.new_project()
        self.w.approve()
        self.w.svc.start_trial("P1")
        self.w.svc.grant_secret_access(
            "P1", "pi", frozenset({K.DATA_CATEGORIES}),
            granter_id="sec", reason="履行个信审查职责",
        )

    def test_dispute_freezes_materials_access_list_and_log(self) -> None:
        w = self.w
        log_len_before = len(w.store.log())
        dispute = w.svc.raise_dispute("P1", raised_by="app", reason="对出境范围有异议（虚构）")

        snapshot = dispute.snapshot
        # 固定的是提出当时的材料集合（每类各 1 个版本）。
        self.assertEqual(len(snapshot.materials), 10)
        self.assertTrue(any(m["trade_secret"] for m in snapshot.materials))
        # 固定了当时的访问名单。
        self.assertEqual([a["person_id"] for a in snapshot.access_list], ["pi"])
        # 固定的是提出之前的操作记录，争议登记本身不在快照内。
        self.assertEqual(len(snapshot.log_entries), log_len_before)
        self.assertEqual(snapshot.log_tail_hash,
                         w.store.log()[log_len_before - 1].entry_hash)

        # 冻结后材料一律不得增删改。
        with self.assertRaises(ImmutableEvidenceError):
            w.svc.submit_material("P1", K.PURPOSES, {"purposes": ["争议期间篡改"]})

    def test_snapshot_includes_revoked_grants_and_is_isolated_from_later_changes(self) -> None:
        w = self.w
        grant = w.store.active_grants("P1")[0]
        w.store.revoke_grant(grant.id, w.clock.now())
        dispute = w.svc.raise_dispute("P1", raised_by="app", reason="授权争议（虚构）")
        self.assertEqual(len(dispute.snapshot.access_list), 1)
        self.assertFalse(dispute.snapshot.access_list[0]["active"])
        self.assertIsNotNone(dispute.snapshot.access_list[0]["revoked_at"])
        # 固定时点材料尚未被打封存标记，快照保留的是提出瞬间的状态。
        self.assertFalse(
            next(m for m in dispute.snapshot.materials if m["kind"] == "purposes")["sealed"]
        )

        # 裁决解冻后追加修订，第一次快照仍是当时的一份版本；
        # 第二次争议固定更新后的集合，两次快照互不串改。
        w.svc.resolve_dispute(
            "P1", dispute.id, DisputeStatus.MEDIATING,
            DisputeOutcome.RESTORE, "调解结束（虚构）", actor="officer",
        )
        w.svc.submit_material("P1", K.PURPOSES, {"purposes": ["裁决后新用途"]})
        first_snapshot_purpose = next(
            m for m in dispute.snapshot.materials if m["kind"] == "purposes"
        )
        self.assertEqual(
            first_snapshot_purpose["content"], MATERIAL_CONTENT[K.PURPOSES]
        )
        new_dispute = w.svc.raise_dispute("P1", raised_by="app", reason="二次争议（虚构）")
        self.assertEqual(
            len([m for m in new_dispute.snapshot.materials if m["kind"] == "purposes"]),
            2,
        )

    def test_evidence_read_requires_duty_or_dispute_officer(self) -> None:
        w = self.w
        dispute = w.svc.raise_dispute("P1", raised_by="app", reason="争议（虚构）")
        with self.assertRaises(AccessDeniedError):
            w.svc.read_evidence("P1", dispute.id, "outsider")
        with self.assertRaises(AccessDeniedError):
            w.svc.read_evidence("P1", dispute.id, "app")
        # 争议解决人员与参与评审的人员可以查阅。
        self.assertIs(w.svc.read_evidence("P1", dispute.id, "officer"), dispute.snapshot)
        self.assertIs(w.svc.read_evidence("P1", dispute.id, "cb"), dispute.snapshot)

    def test_restore_outcome_resumes_and_unseals(self) -> None:
        w = self.w
        w.svc.security_incident("P1", "争议伴随的暂停（虚构）", "sec")
        dispute = w.svc.raise_dispute("P1", raised_by="app", reason="暂停争议（虚构）")
        w.svc.resolve_dispute(
            "P1", dispute.id, DisputeStatus.ARBITRATING,
            DisputeOutcome.RESTORE, "仲裁支持企业（虚构）", actor="officer",
            restore_to=RunStatus.NORMAL,
        )
        project = w.store.get_project("P1")
        self.assertEqual(project.run_status, RunStatus.NORMAL)
        # 解冻后可以补交修订；但快照仍是当时的旧版本集合。
        w.svc.submit_material("P1", K.PURPOSES, {"purposes": ["裁决后修订用途"]})
        self.assertEqual(len(project.materials[K.PURPOSES]), 2)
        sealed_purposes = [m for m in dispute.snapshot.materials if m["kind"] == "purposes"]
        self.assertEqual(len(sealed_purposes), 1)
        self.assertEqual(sealed_purposes[0]["content"], MATERIAL_CONTENT[K.PURPOSES])

    def test_adjust_outcome_creates_amendment_without_touching_approval(self) -> None:
        w = self.w
        original_approval = w.store.get_project("P1").approval
        dispute = w.svc.raise_dispute("P1", raised_by="app", reason="规模争议（虚构）")
        w.svc.resolve_dispute(
            "P1", dispute.id, DisputeStatus.MEDIATING,
            DisputeOutcome.ADJUST, "调解缩减用户规模（虚构）", actor="officer",
        )
        new_boundary = ApprovalBoundary(
            data_scopes=["scope:风控特征"],
            start_at=T0, end_at=T0 + timedelta(days=60), max_users=300,
            monitoring_metrics={"max_daily_users": 300.0, "error_rate": 0.01},
            exit_conditions=["调解期满"],
        )
        amendment = w.svc.apply_dispute_adjustment(
            "P1", dispute.id, new_boundary, actor="officer", note="规模降至300"
        )
        project = w.store.get_project("P1")
        # 原批准决定及其边界不动；有效边界改以修订为准。
        self.assertIs(project.approval, original_approval)
        self.assertEqual(project.approval.boundary.max_users, 1000)
        self.assertEqual(w.svc.effective_boundary(project).max_users, 300)
        view = w.svc.supervisor_view("P1", "cb")
        self.assertEqual(view["approved_boundary"]["max_users"], 300)
        self.assertEqual(view["approved_boundary"]["amended_by"], amendment.id)
        # 修订后按新限值触发整改。
        w.svc.report_metric_reading("P1", "max_daily_users", 800, reporter="app")
        self.assertEqual(project.run_status, RunStatus.RECTIFYING)

    def test_terminate_outcome_keeps_exit_and_preserves_decision(self) -> None:
        w = self.w
        w.svc.partner_withdraws("P1", "合作方退出（虚构）", "cb")
        dispute = w.svc.raise_dispute("P1", raised_by="app", reason="退出是否合法（虚构）")
        w.svc.resolve_dispute(
            "P1", dispute.id, DisputeStatus.LITIGATING,
            DisputeOutcome.TERMINATE, "诉讼维持退出（虚构）", actor="officer",
        )
        project = w.store.get_project("P1")
        self.assertEqual(project.run_status, RunStatus.EXITED)
        self.assertIsNotNone(project.approval)  # 决定仍可追溯
        with self.assertRaises(StateError):
            w.svc.resolve_dispute(
                "P1", dispute.id, DisputeStatus.LITIGATING,
                DisputeOutcome.TERMINATE, "重复裁决", actor="officer",
            )


class OutageDeadlineTest(unittest.TestCase):
    def test_outage_crossing_deadline_postpones_it_and_keeps_evidence(self) -> None:
        w = SandboxWorld()
        w.new_project()
        w.approve()
        w.svc.start_trial("P1")

        # 第 1 天指标超限：整改期限为第 16 天（业务时间）。
        w.manual.advance(days=1)
        w.svc.report_metric_reading("P1", "error_rate", 0.05, reporter="app")
        event = w.store.get_project("P1").events[-1]
        original_deadline = event.deadline

        # 第 2 天服务中断，墙钟走过 10 天后才恢复。
        w.manual.advance(days=1)
        w.svc.service_outage_begin("平台机房故障（虚构）")
        w.manual.advance(days=10)
        downtime = w.svc.service_outage_end(["P1"], reason="故障修复")
        self.assertEqual(downtime, timedelta(days=10))

        # 期限按停顿时长顺延，中断事实与受影响期限均留证。
        self.assertEqual(event.deadline, original_deadline + timedelta(days=10))
        outage = w.store.get_project("P1").outages[-1]
        self.assertEqual(outage.affected_deadlines, [f"event-{event.seq}"])

        # 墙钟已到第 13 天，但业务时间只推进到第 3 天，整改仍可按期解除，
        # 恢复后项目按原计划继续推进。
        w.svc.resume_from_rectification("P1", "整改完成（虚构）", "sec")
        self.assertEqual(w.store.get_project("P1").run_status, RunStatus.NORMAL)
        self.assertTrue(w.store.verify_log_chain())


class RiskAndSupervisorViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SandboxWorld()
        self.w.new_project()
        self.w.approve()
        self.w.svc.start_trial("P1")

    def test_view_shows_boundary_phase_deadline_and_open_risks(self) -> None:
        w = self.w
        risk = w.svc.open_risk("P1", "境外接收方安全资质待补（虚构）", opened_by="sec")
        w.svc.report_metric_reading("P1", "error_rate", 0.09, reporter="app")

        view = w.svc.supervisor_view("P1", "cb")
        self.assertEqual(view["run_status"], "rectifying")
        self.assertEqual(view["approved_boundary"]["data_scopes"],
                         make_boundary().data_scopes)
        self.assertEqual(len(view["open_risks"]), 1)
        self.assertIsNotNone(view["rectification_deadline"])
        self.assertTrue(view["slot_held"])

        w.svc.relieve_risk("P1", risk.id, "资质材料已补全", actor="sec")
        view = w.svc.supervisor_view("P1", "cb")
        self.assertEqual(view["open_risks"], [])

    def test_view_denied_to_unrelated_person(self) -> None:
        with self.assertRaises(AccessDeniedError):
            self.w.svc.supervisor_view("P1", "outsider")

    def test_active_dispute_is_visible(self) -> None:
        self.w.svc.raise_dispute("P1", raised_by="app", reason="争议（虚构）")
        view = self.w.svc.supervisor_view("P1", "officer")
        self.assertTrue(view["active_dispute"].startswith("dispute-"))


if __name__ == "__main__":
    unittest.main()
