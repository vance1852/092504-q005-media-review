import unittest
from datetime import datetime, timedelta, timezone

from skills_workspace.clock import MutableClock
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.review import ReviewService
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


SHA = {
    1: "a" * 64,
    2: "b" * 64,
    3: "c" * 64,
}


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.service = ReviewService(self.base)
        # 基础建档
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="数字媒体训练中心")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op", actor_id="ad1", new_actor_id="op1",
                                 display_name="操作员", role="operator", organization_id="o1")
        self.base.register_actor(request_id="au", actor_id="ad1", new_actor_id="aud1",
                                 display_name="审计员", role="auditor", organization_id="o1")
        # 三名评委：r1 与作者同组织需回避；r2/r3 他组织
        self.base.register_actor(request_id="r1", actor_id="ad1", new_actor_id="r1",
                                 display_name="评委甲", role="reviewer", organization_id="o1")
        self.base.register_organization(request_id="org2", actor_id="ad1",
                                        organization_id="o2", name="外部院校")
        self.base.register_actor(request_id="r2", actor_id="ad1", new_actor_id="r2",
                                 display_name="评委乙", role="reviewer", organization_id="o2")
        self.base.register_actor(request_id="r3", actor_id="ad1", new_actor_id="r3",
                                 display_name="评委丙", role="reviewer", organization_id="o2")
        self.deadline = "2026-09-25T09:00:00Z"
        self.service.create_competition(request_id="comp", actor_id="ad1", competition_id="c1",
                                        title="交互媒体设计赛", deadline=self.deadline)

    def tearDown(self):
        self.database.close()

    # -- 辅助 -------------------------------------------------------------

    def register_work(self, work_id="w1", required=("bgm", "font")):
        return self.service.register_work(
            request_id="work-" + work_id, actor_id="op1", competition_id="c1", work_id=work_id,
            title="星河交互叙事", author_name="张三", required_credentials=list(required),
            author_actor_id="stu1", author_org_id="o1")

    def submit(self, work_id="w1", seq=1, request_id=None, credentials=None):
        return self.service.submit_version(
            request_id=request_id or f"ver-{work_id}-{seq}", actor_id="op1", work_id=work_id,
            package_name="xinghe.zip", package_sha256=SHA[seq],
            script_summary=f"第{seq}版脚本摘要", interaction_notes=f"第{seq}版交互说明",
            metadata={"round": seq}, manifest=[{"item_id": "bgm"}, {"item_id": "font"}],
            credentials=credentials if credentials is not None else {})

    def add_credentials(self, work_id="w1", items=("bgm", "font")):
        for index, item in enumerate(items):
            self.service.register_credential(
                request_id=f"cred-{work_id}-{item}", actor_id="op1", work_id=work_id,
                item_id=item, license_code="CC-BY-4.0",
                evidence_ref=f"evidence/{work_id}/{item}.pdf", evidence_hash="d" * 64)

    def freeze(self):
        self.clock.set(datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc))
        return self.service.freeze_competition(request_id="freeze", actor_id="ad1",
                                               competition_id="c1")

    # -- 版本与凭据 --------------------------------------------------------

    def test_work_starts_pending_evidence(self):
        self.register_work()
        self.assertEqual("pending_evidence", self.service.get_work("w1").status)

    def test_credentials_complete_then_submittable(self):
        self.register_work()
        self.submit()
        self.add_credentials()
        self.assertEqual("submittable", self.service.get_work("w1").status)

    def test_versions_append_and_never_overwrite(self):
        self.register_work()
        self.submit(seq=1)
        self.submit(seq=2, request_id="ver-w1-2")
        versions = self.service.list_versions("w1")
        self.assertEqual([1, 2], [v.sequence_no for v in versions])
        self.assertEqual([SHA[1], SHA[2]], [v.package_sha256 for v in versions])
        # 相同内容不能重复追加
        with self.assertRaises(ConflictError):
            self.submit(seq=1, request_id="ver-w1-again")
        # 旧版本行不可改写：第 1 版摘要保持不变
        self.assertEqual("第1版脚本摘要", versions[0].script_summary)

    def test_credential_is_immutable(self):
        self.register_work()
        self.submit()
        self.add_credentials(items=("bgm",))
        with self.assertRaises(ConflictError):
            self.service.register_credential(
                request_id="cred-bgmagain", actor_id="op1", work_id="w1", item_id="bgm",
                license_code="OTHER", evidence_ref="x", evidence_hash="d" * 64)

    def test_no_version_after_deadline(self):
        self.register_work()
        self.clock.set(datetime(2026, 9, 25, 9, 1, tzinfo=timezone.utc))
        with self.assertRaises(ConflictError):
            self.submit()

    def test_freeze_before_deadline_rejected(self):
        self.register_work()
        self.submit()
        self.add_credentials()
        self.clock.set(datetime(2026, 9, 25, 8, 59, tzinfo=timezone.utc))
        with self.assertRaises(ConflictError):
            self.service.freeze_competition(request_id="freeze-early", actor_id="ad1",
                                            competition_id="c1")
        # 到达截止点即可冻结（边界等于允许）
        self.freeze()
        self.assertEqual("frozen", self.service.get_work("w1").status)

    def test_missing_credentials_quarantined_at_freeze(self):
        self.register_work()
        self.submit()
        self.add_credentials(items=("bgm",))  # 仍缺 font
        self.freeze()
        self.assertEqual("quarantined", self.service.get_work("w1").status)
        # 隔离作品不能被分派评分
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        self.assertEqual([], self.service.list_assignments(competition_id="c1"))

    def test_supplement_and_admit_quarantine(self):
        self.test_missing_credentials_quarantined_at_freeze()
        # 仅补登记凭据还不够：补证准入前状态仍隔离、不参与分派
        self.service.register_credential(
            request_id="cred-w1-font", actor_id="op1", work_id="w1", item_id="font",
            license_code="OFL-1.1", evidence_ref="evidence/w1/font.pdf", evidence_hash="e" * 64)
        self.assertEqual("quarantined", self.service.get_work("w1").status)
        self.assertEqual([], self.service.list_assignments(competition_id="c1"))
        # 经核验准入后才可送审
        self.service.admit_quarantine(request_id="admit", actor_id="ad1", work_id="w1")
        self.assertEqual("frozen", self.service.get_work("w1").status)
        self.service.assign_reviewers(request_id="assign-after-admit", actor_id="ad1",
                                      competition_id="c1")
        self.assertEqual(2, len(self.service.list_assignments(competition_id="c1")))

    def test_frozen_snapshot_is_last_version(self):
        self.register_work()
        self.submit(seq=1)
        self.submit(seq=2, request_id="ver-w1-2")
        self.add_credentials()
        self.freeze()
        work = self.service.get_work("w1")
        self.assertEqual(2, self.service.list_versions("w1")[-1].sequence_no)
        detail = self.service.work_audit_detail(actor_id="aud1", work_id="w1")
        self.assertEqual(work.frozen_version_id, detail["frozen_snapshot"]["version_id"])
        self.assertEqual(SHA[2], detail["frozen_snapshot"]["package_sha256"])

    def test_freeze_blocks_new_versions(self):
        self.register_work()
        self.submit(seq=1)
        self.add_credentials()
        self.freeze()
        with self.assertRaises(ConflictError):
            self.submit(seq=2, request_id="ver-w1-2")

    # -- 分派与回避 --------------------------------------------------------

    def frozen_work(self):
        self.register_work()
        self.submit()
        self.add_credentials()
        self.freeze()

    def test_assignment_excludes_conflicted_reviewers(self):
        self.frozen_work()
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        assignments = self.service.list_assignments(competition_id="c1")
        reviewers = {a.reviewer_actor_id for a in assignments}
        self.assertNotIn("r1", reviewers)  # 同组织自动回避
        self.assertEqual({"r2", "r3"}, reviewers)

    def test_declared_conflict_excludes_reviewer(self):
        self.frozen_work()
        # r2 声明与该作品存在师生关系冲突
        self.service.declare_conflict(request_id="conf", actor_id="ad1",
                                      reviewer_actor_id="r2", conflict_key="w1")
        # r1 同组织、r2 声明冲突，只有 r3 可用，无法凑齐 2 名
        with self.assertRaises(ConflictError):
            self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")

    def test_recuse_backfills_with_same_rules(self):
        self.frozen_work()
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        r2_assignment = next(a for a in self.service.list_assignments(work_id="w1")
                             if a.reviewer_actor_id == "r2")
        # r3 声明冲突后，r2 回避将无人可补
        self.service.declare_conflict(request_id="conf3", actor_id="ad1",
                                      reviewer_actor_id="r3", conflict_key="w1")
        self.service.recuse(request_id="recuse", actor_id="r2",
                            assignment_id=r2_assignment.assignment_id,
                            reason="发现合作关系")
        recused = [a for a in self.service.list_assignments(work_id="w1") if a.status == "recused"]
        self.assertEqual(1, len(recused))
        # r1 同组织、r3 已占用且声明冲突，没有合规评委可补位：在任评委只剩 r3
        active = {a.reviewer_actor_id for a in self.service.list_assignments(work_id="w1")
                  if a.status == "assigned"}
        self.assertEqual({"r3"}, active)

    def test_reviewer_cannot_score_other_assignment(self):
        self.frozen_work()
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        r2_assignment = next(a for a in self.service.list_assignments(work_id="w1")
                             if a.reviewer_actor_id == "r2")
        with self.assertRaises(PermissionDenied):
            self.service.submit_score(
                request_id="s1", actor_id="r3", assignment_id=r2_assignment.assignment_id,
                version_id=self.service.get_work("w1").frozen_version_id,
                dimension="script", points=80, comment="越权评分")

    # -- 评分决定 ----------------------------------------------------------

    def assignments_for(self, work_id="w1"):
        return self.service.list_assignments(work_id=work_id)

    def test_score_must_reference_frozen_version(self):
        self.frozen_work()
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        assignment = self.assignments_for()[0]
        with self.assertRaises(ConflictError):
            self.service.submit_score(
                request_id="s1", actor_id=assignment.reviewer_actor_id,
                assignment_id=assignment.assignment_id, version_id="0" * 32,
                dimension="script", points=80, comment="引用了错误版本")

    def test_duplicate_score_returns_original(self):
        self.frozen_work()
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        assignment = self.assignments_for()[0]
        version_id = self.service.get_work("w1").frozen_version_id
        first, outcome1 = self.service.submit_score(
            request_id="s1", actor_id=assignment.reviewer_actor_id,
            assignment_id=assignment.assignment_id, version_id=version_id,
            dimension="script", points=80, comment="良好")
        self.assertEqual("created", outcome1)
        # 同一请求重放
        replay, outcome2 = self.service.submit_score(
            request_id="s1", actor_id=assignment.reviewer_actor_id,
            assignment_id=assignment.assignment_id, version_id=version_id,
            dimension="script", points=80, comment="良好")
        self.assertEqual("replayed", outcome2)
        self.assertEqual(first.score_id, replay.score_id)
        # 不带相同 request_id 的重复评分也返回原决定
        dup, outcome3 = self.service.submit_score(
            request_id="s1-dup", actor_id=assignment.reviewer_actor_id,
            assignment_id=assignment.assignment_id, version_id=version_id,
            dimension="script", points=80, comment="良好")
        self.assertEqual("duplicate", outcome3)
        self.assertEqual(first.score_id, dup.score_id)

    def test_changed_points_same_request_exposes_conflict(self):
        self.frozen_work()
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        assignment = self.assignments_for()[0]
        version_id = self.service.get_work("w1").frozen_version_id
        kwargs = dict(actor_id=assignment.reviewer_actor_id,
                      assignment_id=assignment.assignment_id, version_id=version_id,
                      dimension="script", comment="良好")
        self.service.submit_score(request_id="s1", points=80, **kwargs)
        with self.assertRaises(ConflictError):
            self.service.submit_score(request_id="s1", points=90, **kwargs)
        with self.assertRaises(ConflictError):
            self.service.submit_score(request_id="s2", points=90, **kwargs)

    # -- 申诉 --------------------------------------------------------------

    def scored_work(self):
        self.frozen_work()
        self.service.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        assignments = self.assignments_for()
        version_id = self.service.get_work("w1").frozen_version_id
        for assignment in assignments:
            self.service.submit_score(
                request_id="score-" + assignment.assignment_id,
                actor_id=assignment.reviewer_actor_id, assignment_id=assignment.assignment_id,
                version_id=version_id, dimension="overall", points=50, comment="偏低")
        return assignments[0]

    def test_appeal_keeps_original_and_review_and_rules_final(self):
        assignment = self.scored_work()
        self.service.open_appeal(request_id="appeal", actor_id="op1", work_id="w1",
                                 reason="对总分有异议")
        # 复核分 55，差距 5 < 阈值 10，维持原分
        self.service.submit_appeal_review(
            request_id="rev", actor_id=self._review_for(assignment), appeal_id=self._appeal_id(),
            assignment_id=assignment.assignment_id, dimension="overall",
            review_points=55, rationale="复核后基本维持")
        self.service.decide_appeal(request_id="decide", actor_id="ad1",
                                   appeal_id=self._appeal_id())
        decisions = self.service.list_decisions("w1")
        target = next(d for d in decisions if d["assignment_id"] == assignment.assignment_id)
        self.assertEqual(50, target["points"])
        self.assertEqual(50, target["final_points"])
        self.assertEqual("kept", target["appeal_outcome"])

    def test_appeal_changes_final_when_threshold_reached(self):
        assignment = self.scored_work()
        self.service.open_appeal(request_id="appeal", actor_id="op1", work_id="w1",
                                 reason="分数明显偏低")
        self.service.submit_appeal_review(
            request_id="rev", actor_id=self._review_for(assignment), appeal_id=self._appeal_id(),
            assignment_id=assignment.assignment_id, dimension="overall",
            review_points=70, rationale="差距显著，采用复核分")
        self.service.decide_appeal(request_id="decide", actor_id="ad1",
                                   appeal_id=self._appeal_id())
        decisions = self.service.list_decisions("w1")
        target = next(d for d in decisions if d["assignment_id"] == assignment.assignment_id)
        # 原分保留，最终分变为复核分
        self.assertEqual(50, target["points"])
        self.assertEqual(70, target["final_points"])
        self.assertEqual("changed", target["appeal_outcome"])

    def test_reviewer_cannot_review_own_decision(self):
        assignment = self.scored_work()
        self.service.open_appeal(request_id="appeal", actor_id="op1", work_id="w1", reason="x")
        with self.assertRaises(PermissionDenied):
            self.service.submit_appeal_review(
                request_id="rev", actor_id=assignment.reviewer_actor_id,
                appeal_id=self._appeal_id(), assignment_id=assignment.assignment_id,
                dimension="overall", review_points=60, rationale="自我复核被拒绝")
        # 非评审组成员（同组织但未分派的 r1）也不能复核
        with self.assertRaises(PermissionDenied):
            self.service.submit_appeal_review(
                request_id="rev2", actor_id="r1", appeal_id=self._appeal_id(),
                assignment_id=assignment.assignment_id, dimension="overall",
                review_points=60, rationale="非评审组成员被拒绝")

    def test_decided_appeal_is_locked(self):
        self.scored_work()
        self.service.open_appeal(request_id="appeal", actor_id="op1", work_id="w1", reason="x")
        appeal_id = self._appeal_id()
        assignment = self.assignments_for()[0]
        reviewer = self._review_for(assignment)
        self.service.submit_appeal_review(
            request_id="rev", actor_id=reviewer, appeal_id=appeal_id,
            assignment_id=assignment.assignment_id, dimension="overall",
            review_points=70, rationale="x")
        self.service.decide_appeal(request_id="decide", actor_id="ad1", appeal_id=appeal_id)
        with self.assertRaises(ConflictError):
            self.service.submit_appeal_review(
                request_id="rev2", actor_id=reviewer, appeal_id=appeal_id,
                assignment_id=assignment.assignment_id, dimension="overall",
                review_points=72, rationale="不能再加")

    def _appeal_id(self):
        detail = self.service.work_audit_detail(actor_id="ad1", work_id="w1")
        return detail["appeal"]["appeal_id"]

    def _review_for(self, assignment):
        """返回同一评审组中可对该分派做独立复核的另一名评委。"""

        other = next(a for a in self.assignments_for()
                     if a.assignment_id != assignment.assignment_id and a.status == "assigned")
        return other.reviewer_actor_id

    # -- 查询与审计 --------------------------------------------------------

    def test_public_status_is_masked(self):
        self.scored_work()
        public = self.service.public_status("w1")
        self.assertNotIn("张三", str(public))
        self.assertTrue(public["author_masked"].startswith("张"))
        self.assertIn("＊", public["author_masked"])
        self.assertNotIn("comment", str(public))
        self.assertIsNotNone(public["overall_average"])
        self.assertEqual("under_review", public["status"])

    def test_audit_trail_covers_credentials_assignment_and_every_decision(self):
        self.scored_work()
        detail = self.service.work_audit_detail(actor_id="aud1", work_id="w1")
        self.assertEqual(2, len(detail["credentials"]))
        self.assertEqual(2, len(detail["assignments"]))
        self.assertEqual(2, len(detail["scores"]))
        self.assertIsNotNone(detail["frozen_snapshot"])
        actions = {event["action"] for event in detail["audit_events"]}
        self.assertIn("credential.registered", actions)
        self.assertIn("reviewer.assigned", actions)
        self.assertIn("score.submitted", actions)
        self.assertIn("snapshot.frozen", actions)

    def test_audit_requires_privileged_role(self):
        self.frozen_work()
        with self.assertRaises(PermissionDenied):
            self.service.work_audit_detail(actor_id="r2", work_id="w1")

    def test_audit_chain_stays_valid(self):
        self.scored_work()
        valid, count = self.base.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 10)


if __name__ == "__main__":
    unittest.main()
