import unittest
from datetime import datetime, timezone

from skills_workspace.api import route
from skills_workspace.clock import MutableClock
from skills_workspace.review import ReviewService
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


def sha(seed):
    return (seed * 64)[:64]


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = MutableClock(datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.review = ReviewService(self.base)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="中心")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="r1", actor_id="ad1", new_actor_id="r1",
                                 display_name="评委", role="reviewer", organization_id="o1")
        self.review.create_competition(request_id="comp", actor_id="ad1", competition_id="c1",
                                       title="赛事", deadline="2026-09-25T09:00:00Z")

    def tearDown(self):
        self.database.close()

    def test_register_work_returns_receipt(self):
        status, payload = route(self.base, "POST", "/works", {
            "request_id": "w1", "competition_id": "c1", "work_id": "w1",
            "title": "作品", "author_name": "张三", "required_credentials": ["bgm"],
            "author_org_id": "o1"}, {"X-Actor-Id": "ad1"})
        self.assertEqual(201, status)
        self.assertEqual("work", payload["resource_type"])
        self.assertFalse(payload["replayed"])

    def test_public_status_available_without_privileged_actor(self):
        route(self.base, "POST", "/works", {
            "request_id": "w1", "competition_id": "c1", "work_id": "w1",
            "title": "作品", "author_name": "张三", "required_credentials": ["bgm"]},
            {"X-Actor-Id": "ad1"})
        status, payload = route(self.base, "GET", "/works/w1/public", None)
        self.assertEqual(200, status)
        self.assertEqual("张＊", payload["author_masked"])

    def test_audit_detail_requires_actor(self):
        route(self.base, "POST", "/works", {
            "request_id": "w1", "competition_id": "c1", "work_id": "w1",
            "title": "作品", "author_name": "张三", "required_credentials": ["bgm"]},
            {"X-Actor-Id": "ad1"})
        status, payload = route(self.base, "GET", "/works/w1/audit", None,
                                {"X-Actor-Id": "r1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_submit_version_validates_hash(self):
        route(self.base, "POST", "/works", {
            "request_id": "w1", "competition_id": "c1", "work_id": "w1",
            "title": "作品", "author_name": "张三", "required_credentials": ["bgm"]},
            {"X-Actor-Id": "ad1"})
        status, payload = route(self.base, "POST", "/work-versions", {
            "request_id": "v1", "work_id": "w1", "package_name": "x.zip",
            "package_sha256": "not-a-hash", "script_summary": "摘要",
            "interaction_notes": "交互", "metadata": {"round": 1},
            "manifest": [{"item_id": "bgm"}], "credentials": {}},
            {"X-Actor-Id": "ad1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_full_review_cycle_over_http(self):
        # 登记作品、提交版本、补凭据
        self.assertEqual(201, route(self.base, "POST", "/works", {
            "request_id": "w1", "competition_id": "c1", "work_id": "w1", "title": "作品",
            "author_name": "张三", "required_credentials": ["bgm"],
            "author_actor_id": "stu", "author_org_id": "o1"},
            {"X-Actor-Id": "ad1"})[0])
        self.assertEqual(201, route(self.base, "POST", "/work-versions", {
            "request_id": "v1", "work_id": "w1", "package_name": "x.zip",
            "package_sha256": sha("a"), "script_summary": "脚本",
            "interaction_notes": "交互", "metadata": {"round": 1},
            "manifest": [{"item_id": "bgm"}], "credentials": {}},
            {"X-Actor-Id": "ad1"})[0])
        self.assertEqual(201, route(self.base, "POST", "/credentials", {
            "request_id": "c1c", "work_id": "w1", "item_id": "bgm", "license_code": "CC-BY-4.0",
            "evidence_ref": "evidence/bgm.pdf", "evidence_hash": sha("d")},
            {"X-Actor-Id": "ad1"})[0])
        # 截止冻结
        status, freeze = route(self.base, "POST", "/freeze",
                               {"request_id": "fz", "competition_id": "c1"},
                               {"X-Actor-Id": "ad1"})
        self.assertEqual(201, status)
        self.assertEqual("competition", freeze["resource_type"])
        self.assertFalse(freeze["replayed"])
        # 公开查询显示评审中
        status, public = route(self.base, "GET", "/works/w1/public", None)
        self.assertEqual("under_review", public["status"])


if __name__ == "__main__":
    unittest.main()
