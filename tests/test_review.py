import unittest
from datetime import datetime, timedelta, timezone

from skills_workspace.errors import ConflictError, PermissionDenied, ValidationError
from skills_workspace.review import ReviewService
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


class MutableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def script(digest):
    return {"synopsis": "讲述一段交互叙事", "script_digest": digest, "revision_note": ""}


def notes():
    return {"summary": "包含三个分支入口的交互说明", "entry_points": ["start", "branch-a"]}


def declaration():
    return {"accepted": True, "signature_text": "作者签名：张"}


def checklist():
    return {"script": True, "material_manifest": True, "interaction_notes": True,
            "author_declaration": True, "evidence_package": True}


def materials_complete():
    return [
        {"material_id": "m1", "name": "背景图", "license": "owned", "credential_digest": "", "source": "自制"},
        {"material_id": "m2", "name": "授权配乐", "license": "licensed",
         "credential_digest": "a" * 64, "source": "音乐库"},
        {"material_id": "m3", "name": "音效", "license": "cc0", "credential_digest": "", "source": "公共领域"},
    ]


def materials_pending():
    return [
        {"material_id": "m1", "name": "来源不明视频", "license": "unknown",
         "credential_digest": "", "source": ""},
        {"material_id": "m2", "name": "授权字体", "license": "permission",
         "credential_digest": "", "source": "厂商"},
    ]


RUBRIC = [
    {"dimension_id": "script", "name": "脚本", "max_score": 100},
    {"dimension_id": "material", "name": "素材规范", "max_score": 100},
    {"dimension_id": "interaction", "name": "交互设计", "max_score": 100},
]


def dims(a, b, c):
    return {"script": a, "material": b, "interaction": c}


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.service = ReviewService(self.database, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        b = self.base
        b.register_organization(request_id="org-main", actor_id="bootstrap",
                                organization_id="org-main", name="主办方")
        b.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                         display_name="管理员", role="admin", organization_id="org-main")
        b.register_organization(request_id="org-rev", actor_id="admin1",
                                organization_id="org-rev", name="评委机构")
        b.register_organization(request_id="org-author", actor_id="admin1",
                                organization_id="org-author", name="作者机构甲")
        b.register_organization(request_id="org-author2", actor_id="admin1",
                                organization_id="org-author2", name="作者机构乙")
        b.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                         display_name="运营", role="operator", organization_id="org-main")
        b.register_actor(request_id="au", actor_id="admin1", new_actor_id="au1",
                         display_name="审计员", role="auditor", organization_id="org-main")
        for index in range(1, 6):
            b.register_actor(request_id=f"r{index}", actor_id="admin1", new_actor_id=f"rv{index}",
                             display_name=f"评委{index}", role="reviewer", organization_id="org-rev")
        b.register_actor(request_id="author1", actor_id="admin1", new_actor_id="author1",
                         display_name="作者甲", role="author", organization_id="org-author")
        b.register_actor(request_id="author2", actor_id="admin1", new_actor_id="author2",
                         display_name="作者乙", role="author", organization_id="org-author2")
        b.register_actor(request_id="author3", actor_id="admin1", new_actor_id="author3",
                         display_name="作者丙", role="author", organization_id="org-author2")

    def _competition(self, competition_id="c1", reviewers=2, tolerance=10.0):
        return self.service.register_competition(
            request_id=f"comp-{competition_id}", actor_id="op1", competition_id=competition_id,
            title="数字交互媒体设计赛", deadline="2026-09-10T00:00:00+00:00",
            reviewers_per_work=reviewers, score_tolerance=tolerance, rubric=RUBRIC)

    def _work(self, work_id, author):
        return self.service.register_work(
            request_id=f"work-{work_id}", actor_id="op1", competition_id="c1",
            work_id=work_id, title=f"作品{work_id}", pseudonym=f"化名{work_id}",
            author_actor_id=author)

    def _submit(self, request_id, actor, work_id, package, digest, materials):
        return self.service.submit_version(
            request_id=request_id, actor_id=actor, work_id=work_id, package_name=package,
            content_digest=digest, script_summary=script("b" * 64),
            material_manifest=materials, interaction_notes=notes(),
            author_declaration=declaration(), delivery_checklist=checklist())

    def _frozen_submission(self, work_id):
        row = self.database.connection.execute(
            "SELECT submission_id FROM snapshot_versions sv JOIN snapshots s ON s.snapshot_id=sv.snapshot_id"
            " WHERE s.competition_id='c1' AND sv.work_id=?", (work_id,)).fetchone()
        return row["submission_id"]

    def _freeze(self):
        self.clock.advance(days=10)
        return self.service.freeze_competition(request_id="freeze-c1", actor_id="op1",
                                               competition_id="c1")

    # ------------------------------------------------------------------

    def test_versions_are_append_only_and_distinguished_by_digest(self):
        self._competition()
        self._work("w1", "author1")
        first = self._submit("sub-1", "author1", "w1", "作品.zip", "c" * 64, materials_complete())
        self.assertFalse(first.replayed)
        version_no = self.database.connection.execute(
            "SELECT version_no FROM submissions WHERE submission_id=?",
            (first.resource_id,)).fetchone()["version_no"]
        self.assertEqual(1, version_no)
        # 同名压缩包、不同内容 -> 追加为 v2
        second = self._submit("sub-2", "author1", "w1", "作品.zip", "d" * 64, materials_complete())
        self.assertFalse(second.replayed)
        rows = self.database.connection.execute(
            "SELECT version_no FROM submissions WHERE work_id='w1' ORDER BY version_no").fetchall()
        self.assertEqual([1, 2], [row["version_no"] for row in rows])
        # 相同内容摘要不能再次登记（没有改写旧版本的路径）
        with self.assertRaises(ConflictError):
            self._submit("sub-3", "author1", "w1", "作品.zip", "d" * 64, materials_complete())

    def test_submit_request_replays_and_conflicts(self):
        self._competition()
        self._work("w1", "author1")
        first = self._submit("sub-1", "author1", "w1", "作品.zip", "c" * 64, materials_complete())
        replay = self._submit("sub-1", "author1", "w1", "作品.zip", "c" * 64, materials_complete())
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.service.submit_version(
                request_id="sub-1", actor_id="author1", work_id="w1", package_name="作品.zip",
                content_digest="e" * 64, script_summary=script("b" * 64),
                material_manifest=materials_complete(), interaction_notes=notes(),
                author_declaration=declaration(), delivery_checklist=checklist())

    def test_cannot_freeze_before_deadline_or_append_after(self):
        self._competition()
        self._work("w1", "author1")
        self._submit("sub-1", "author1", "w1", "作品.zip", "c" * 64, materials_complete())
        with self.assertRaises(ConflictError):
            self.service.freeze_competition(request_id="freeze-early", actor_id="op1",
                                            competition_id="c1")
        self._freeze()
        with self.assertRaises(ConflictError):
            self._submit("sub-late", "author1", "w1", "作品.zip", "f" * 64, materials_complete())

    def test_missing_credentials_enters_pending_evidence_and_is_not_assigned(self):
        self._competition(reviewers=2)
        self._work("w1", "author1")
        self._work("w2", "author2")
        self._submit("sub-w1", "author1", "w1", "a.zip", "c" * 64, materials_complete())
        pending = self._submit("sub-w2", "author2", "w2", "a.zip", "c" * 64, materials_pending())
        self.assertEqual("submission", pending.resource_type)
        gap_status = self.database.connection.execute(
            "SELECT evidence_status FROM submissions WHERE work_id='w2'").fetchone()["evidence_status"]
        self.assertEqual("pending_evidence", gap_status)
        receipt = self._freeze()
        self.assertFalse(receipt.replayed)
        snap = self.database.connection.execute(
            "SELECT * FROM snapshot_versions WHERE work_id='w2'").fetchone()
        self.assertEqual("pending_evidence", snap["evidence_status"])
        self.service.register_conflict(request_id="cf1", actor_id="rv1", reviewer_id="rv1",
                                       work_id="w1", reason="指导过该作品")
        assigned = self.service.assign_reviewers(request_id="assign-1", actor_id="op1",
                                                 competition_id="c1")
        # 只分派给凭据完整的 w1，且 rv1 因利益冲突被排除
        work_map = {item["work_id"]: item["reviewer_ids"] for item in self._assignment_payload(assigned)}
        self.assertNotIn("w2", work_map)
        self.assertNotIn("rv1", work_map["w1"])
        self.assertEqual(2, len(work_map["w1"]))
        # 待补证作品不能评分
        with self.assertRaises(ConflictError):
            self.service.submit_score(request_id="score-x", actor_id="rv2", work_id="w2",
                                      submission_id=self._frozen_submission("w2"),
                                      dimensions=dims(80, 80, 80), rationale="评语")

    def _assignment_payload(self, receipt):
        row = self.database.connection.execute(
            "SELECT response_json FROM request_receipts WHERE resource_type='review_assignment'")\
            .fetchone()
        import json
        return json.loads(row["response_json"])["assignments"]

    def test_same_organization_reviewer_is_excluded(self):
        # 评委机构与作者机构相同的唯一评委不能被分派
        self.base.register_organization(request_id="org-solo", actor_id="admin1",
                                        organization_id="org-solo", name="独立机构")
        self.base.register_actor(request_id="rsolo", actor_id="admin1", new_actor_id="rvsolo",
                                 display_name="同机构评委", role="reviewer",
                                 organization_id="org-author")
        self._competition(reviewers=6)
        self._work("w1", "author1")
        self._submit("sub-w1", "author1", "w1", "a.zip", "c" * 64, materials_complete())
        self._freeze()
        with self.assertRaises(ValidationError):
            self.service.assign_reviewers(request_id="assign-fail", actor_id="op1",
                                          competition_id="c1")

    def test_score_must_reference_frozen_version_and_is_immutable(self):
        self._competition()
        self._work("w1", "author1")
        self._submit("sub-v1", "author1", "w1", "作品.zip", "c" * 64, materials_complete())
        self._submit("sub-v2", "author1", "w1", "作品.zip", "d" * 64, materials_complete())
        self._freeze()
        self.service.register_conflict(request_id="cf1", actor_id="rv1", reviewer_id="rv1",
                                       work_id="w1", reason="亲属关系")
        self.service.assign_reviewers(request_id="assign-1", actor_id="op1", competition_id="c1")
        frozen_v2 = self._frozen_submission("w1")
        v1 = self.database.connection.execute(
            "SELECT submission_id FROM submissions WHERE work_id='w1' AND version_no=1").fetchone()["submission_id"]
        # 必须引用冻结的 v2，引用旧 v1 被拒绝
        with self.assertRaises(ValidationError):
            self.service.submit_score(request_id="score-bad-version", actor_id="rv2", work_id="w1",
                                      submission_id=v1, dimensions=dims(80, 80, 80), rationale="评语")
        first = self.service.submit_score(request_id="score-r2", actor_id="rv2", work_id="w1",
                                          submission_id=frozen_v2,
                                          dimensions=dims(80, 85, 90), rationale="完成度高")
        self.assertEqual(255.0, first.total)
        # 重复评分请求返回原决定
        replay = self.service.submit_score(request_id="score-r2", actor_id="rv2", work_id="w1",
                                           submission_id=frozen_v2,
                                           dimensions=dims(80, 85, 90), rationale="完成度高")
        self.assertTrue(replay.replayed)
        self.assertEqual(255.0, replay.total)
        self.assertEqual(first.decision_id, replay.decision_id)
        # 不同分值复用同一请求编号 -> 冲突暴露
        with self.assertRaises(ConflictError):
            self.service.submit_score(request_id="score-r2", actor_id="rv2", work_id="w1",
                                      submission_id=frozen_v2,
                                      dimensions=dims(20, 20, 20), rationale="篡改")
        # 新的请求编号也不能改写既有决定
        with self.assertRaises(ConflictError):
            self.service.submit_score(request_id="score-r2-again", actor_id="rv2", work_id="w1",
                                      submission_id=frozen_v2,
                                      dimensions=dims(50, 50, 50), rationale="改分")
        # 未分派评委不能评分
        with self.assertRaises(PermissionDenied):
            self.service.submit_score(request_id="score-r1", actor_id="rv1", work_id="w1",
                                      submission_id=frozen_v2,
                                      dimensions=dims(80, 80, 80), rationale="评语")

    def test_recused_reviewer_is_replaced_and_cannot_review_appeal(self):
        self._competition()
        self._work("w1", "author1")
        self._submit("sub-w1", "author1", "w1", "a.zip", "c" * 64, materials_complete())
        self._freeze()
        self.service.register_conflict(request_id="cf1", actor_id="rv1", reviewer_id="rv1",
                                       work_id="w1", reason="利益冲突")
        self.service.assign_reviewers(request_id="assign-1", actor_id="op1", competition_id="c1")
        # rv2、rv3 被分派，rv2 主动回避
        recuse_id = self.database.connection.execute(
            "SELECT assignment_id FROM review_assignments WHERE work_id='w1' AND reviewer_id='rv2'")\
            .fetchone()["assignment_id"]
        recusal = self.service.recuse_assignment(request_id="recuse-1", actor_id="rv2",
                                                 assignment_id=recuse_id, reason="曾合作")
        self.assertTrue(recusal.resource_id)
        replay = self.service.recuse_assignment(request_id="recuse-1", actor_id="rv2",
                                                assignment_id=recuse_id, reason="曾合作")
        self.assertTrue(replay.replayed)
        # 补员：rv1 冲突、rv2 已回避、rv3 已在任 -> 只能补 rv4
        self.service.assign_reviewers(request_id="assign-2", actor_id="op1", competition_id="c1")
        reviewers = {row["reviewer_id"]: row["status"] for row in self.database.connection.execute(
            "SELECT reviewer_id, status FROM review_assignments WHERE work_id='w1'")}
        self.assertEqual("recused", reviewers["rv2"])
        self.assertEqual("assigned", reviewers["rv3"])
        self.assertEqual("assigned", reviewers["rv4"])
        frozen = self._frozen_submission("w1")
        self.service.submit_score(request_id="s-rv3", actor_id="rv3", work_id="w1",
                                  submission_id=frozen, dimensions=dims(90, 90, 90), rationale="优")
        self.service.submit_score(request_id="s-rv4", actor_id="rv4", work_id="w1",
                                  submission_id=frozen, dimensions=dims(80, 80, 80), rationale="良")
        self.service.file_appeal(request_id="appeal-1", actor_id="author1",
                                 work_id="w1", reason="评分偏低")
        with self.assertRaises(PermissionDenied):
            self.service.submit_appeal_review_score(
                request_id="ar-rv2", actor_id="rv2", appeal_id=self._appeal_id("w1"),
                submission_id=frozen, dimensions=dims(85, 85, 85), rationale="复核")

    def _appeal_id(self, work_id):
        return self.database.connection.execute(
            "SELECT appeal_id FROM appeals WHERE work_id=?", (work_id,)).fetchone()["appeal_id"]

    def test_appeal_keeps_both_scores_and_applies_tolerance_rules(self):
        self._competition(tolerance=10.0)
        # w1: 复核分超出容差 -> 采用复核分
        self._work("w1", "author1")
        self._submit("sub-w1", "author1", "w1", "a.zip", "c" * 64, materials_complete())
        # w3: 复核分在容差内 -> 维持原分
        self._work("w3", "author3")
        self._submit("sub-w3", "author3", "w3", "b.zip", "9" * 64, materials_complete())
        self._freeze()
        self.service.assign_reviewers(request_id="assign-1", actor_id="op1", competition_id="c1")

        f1, f3 = self._frozen_submission("w1"), self._frozen_submission("w3")
        reviewers_w1 = [row["reviewer_id"] for row in self.database.connection.execute(
            "SELECT reviewer_id FROM review_assignments WHERE work_id='w1' AND status='assigned'")]
        reviewers_w3 = [row["reviewer_id"] for row in self.database.connection.execute(
            "SELECT reviewer_id FROM review_assignments WHERE work_id='w3' AND status='assigned'")]
        a, b = reviewers_w1
        self.service.submit_score(request_id="w1-s1", actor_id=a, work_id="w1",
                                  submission_id=f1, dimensions=dims(80, 80, 80), rationale="平")
        self.service.submit_score(request_id="w1-s2", actor_id=b, work_id="w1",
                                  submission_id=f1, dimensions=dims(90, 90, 90), rationale="良")
        c, d = reviewers_w3
        self.service.submit_score(request_id="w3-s1", actor_id=c, work_id="w3",
                                  submission_id=f3, dimensions=dims(70, 70, 72), rationale="平")
        self.service.submit_score(request_id="w3-s2", actor_id=d, work_id="w3",
                                  submission_id=f3, dimensions=dims(70, 70, 74), rationale="平")

        self.service.file_appeal(request_id="ap-w1", actor_id="author1", work_id="w1",
                                 reason="认为分数偏低")
        appeal1 = self._appeal_id("w1")
        # 原审评委不能复核自己作品
        with self.assertRaises(PermissionDenied):
            self.service.submit_appeal_review_score(
                request_id="ar-self", actor_id=a, appeal_id=appeal1, submission_id=f1,
                dimensions=dims(95, 95, 98), rationale="复核")
        # 复核也必须引用冻结版本
        with self.assertRaises(ValidationError):
            self.service.submit_appeal_review_score(
                request_id="ar-badver", actor_id="rv5", appeal_id=appeal1,
                submission_id="wrong", dimensions=dims(95, 95, 98), rationale="复核")
        # 找一名非原审评委复核：gap = 96 - 85 = 11 > 10
        other1 = next(r for r in ("rv1", "rv2", "rv3", "rv4", "rv5")
                      if r not in reviewers_w1)
        ar = self.service.submit_appeal_review_score(
            request_id="ar-w1", actor_id=other1, appeal_id=appeal1, submission_id=f1,
            dimensions=dims(95, 95, 98), rationale="应更高")
        self.assertEqual(288.0, ar.total)
        # 无复核分时不能裁决
        self.service.file_appeal(request_id="ap-w3", actor_id="author3", work_id="w3",
                                 reason="申诉")
        appeal3 = self._appeal_id("w3")
        with self.assertRaises(ConflictError):
            self.service.rule_appeal(request_id="rule-w3-early", actor_id="op1", appeal_id=appeal3)
        other3 = next(r for r in ("rv1", "rv2", "rv3", "rv4", "rv5")
                      if r not in reviewers_w3 and r != other1)
        # 原审均值 = 213/... 实际总分 210 与 216，均值 213；复核总分 214... 改为容差内：
        # 原审总分均值 = (210+216)/2 = 213，复核 218，gap 5 <= 10 -> 维持
        self.service.submit_appeal_review_score(
            request_id="ar-w3", actor_id=other3, appeal_id=appeal3, submission_id=f3,
            dimensions=dims(72, 73, 73), rationale="基本一致")
        ruled1 = self.service.rule_appeal(request_id="rule-w1", actor_id="op1", appeal_id=appeal1)
        ruled3 = self.service.rule_appeal(request_id="rule-w3", actor_id="op1", appeal_id=appeal3)
        self.assertFalse(ruled1.replayed)
        final1 = self.database.connection.execute(
            "SELECT final_total, final_basis FROM appeals WHERE appeal_id=?", (appeal1,)).fetchone()
        final3 = self.database.connection.execute(
            "SELECT final_total, final_basis FROM appeals WHERE appeal_id=?", (appeal3,)).fetchone()
        self.assertEqual(288.0, final1["final_total"])
        self.assertEqual("review_applied", final1["final_basis"])
        self.assertEqual(213.0, final3["final_total"])
        self.assertEqual("original_upheld", final3["final_basis"])
        # 裁决不可改写
        with self.assertRaises(ConflictError):
            self.service.rule_appeal(request_id="rule-w1-again", actor_id="op1", appeal_id=appeal1)
        # 原分与复核分都仍可查到
        originals = self.database.connection.execute(
            "SELECT total FROM score_decisions sd JOIN review_assignments ra"
            " ON ra.assignment_id=sd.assignment_id WHERE ra.work_id='w1'").fetchall()
        self.assertEqual({240.0, 270.0}, {row["total"] for row in originals})
        reviews = self.database.connection.execute(
            "SELECT total FROM appeal_reviews WHERE appeal_id=?", (appeal1,)).fetchall()
        self.assertEqual([288.0], [row["total"] for row in reviews])

    def test_public_view_is_masked_and_audit_view_is_full(self):
        self._competition()
        self._work("w1", "author1")
        self._submit("sub-w1", "author1", "w1", "作品.zip", "c" * 64, materials_complete())
        self._freeze()
        self.service.assign_reviewers(request_id="assign-1", actor_id="op1", competition_id="c1")
        frozen = self._frozen_submission("w1")
        reviewers = [row["reviewer_id"] for row in self.database.connection.execute(
            "SELECT reviewer_id FROM review_assignments WHERE work_id='w1' AND status='assigned'")]
        self.service.submit_score(request_id="s1", actor_id=reviewers[0], work_id="w1",
                                  submission_id=frozen, dimensions=dims(80, 80, 80), rationale="平")
        self.service.submit_score(request_id="s2", actor_id=reviewers[1], work_id="w1",
                                  submission_id=frozen, dimensions=dims(90, 90, 90), rationale="良")

        public = self.service.public_work("w1")
        import json
        public_text = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("author1", public_text)
        self.assertNotIn("rv", public_text)
        self.assertNotIn(" rationale", public_text)
        self.assertEqual("化名w1", public["pseudonym"])
        self.assertEqual(2, public["scores"]["reviewer_count"])
        self.assertEqual(255.0, public["scores"]["average_total"])
        self.assertEqual(1, public["frozen_version"]["version_no"])
        self.assertEqual("scored", public["status"])

        # 审计视图仅限 admin、auditor
        with self.assertRaises(PermissionDenied):
            self.service.audit_work(actor_id="author1", work_id="w1")
        trace = self.service.audit_work(actor_id="au1", work_id="w1")
        self.assertEqual("author1", trace["work"]["author_actor_id"])
        self.assertEqual("a" * 64, trace["submissions"][0]["material_manifest"][1]
                         ["credential_digest"])
        self.assertEqual(2, len(trace["assignments"]))
        self.assertEqual(2, len(trace["decisions"]))
        self.assertTrue(all(d["request_id"] for d in trace["decisions"]))
        self.assertEqual(1, trace["snapshot"]["frozen_version_no"])
        actions = {event["action"] for event in trace["audit_events"]}
        self.assertIn("score.decided", actions)
        self.assertIn("competition.frozen", actions)
        self.assertIn("reviewers.assigned", actions)

    def test_pending_work_shows_masked_status_publicly(self):
        self._competition()
        self._work("w2", "author2")
        self._submit("sub-w2", "author2", "w2", "a.zip", "c" * 64, materials_pending())
        self._freeze()
        public = self.service.public_work("w2")
        self.assertEqual("pending_evidence", public["status"])
        self.assertIsNone(public["scores"])
        self.assertEqual("pending_evidence", public["frozen_version"]["evidence_status"])

    def test_auditor_cannot_mutate_and_author_can_only_self_submit(self):
        self._competition()
        self._work("w1", "author1")
        with self.assertRaises(PermissionDenied):
            self.service.register_competition(
                request_id="x", actor_id="au1", competition_id="c2", title="t",
                deadline="2026-09-10T00:00:00+00:00", rubric=RUBRIC)
        # 作者不能替别人的作品提交
        self._work("w2", "author2")
        with self.assertRaises(PermissionDenied):
            self._submit("cheat", "author1", "w2", "a.zip", "c" * 64, materials_complete())


if __name__ == "__main__":
    unittest.main()
