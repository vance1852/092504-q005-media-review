import unittest
from datetime import datetime, timezone

from skills_workspace.api import route
from skills_workspace.clock import FixedClock
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


RUBRIC = [{"dimension_id": "d1", "name": "维度一", "max_score": 100}]


def checklist():
    return {"script": True, "material_manifest": True, "interaction_notes": True,
            "author_declaration": True, "evidence_package": True}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="主办")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_organization(request_id="org-a", actor_id="a1",
                                           organization_id="oa", name="作者机构")
        self.service.register_actor(request_id="author", actor_id="a1", new_actor_id="au",
                                    display_name="作者", role="author", organization_id="oa")

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_competition_lifecycle_over_http(self):
        status, payload = route(self.service, "POST", "/competitions", {
            "request_id": "c1", "competition_id": "comp1", "title": "展评",
            "deadline": "2026-09-10T00:00:00+00:00", "reviewers_per_work": 1,
            "rubric": RUBRIC}, {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        self.assertEqual("comp1", payload["resource_id"])

        status, payload = route(self.service, "POST", "/works", {
            "request_id": "w1", "competition_id": "comp1", "work_id": "work1",
            "title": "作品", "pseudonym": "化名", "author_actor_id": "au"},
            {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)

        status, payload = route(self.service, "POST", "/submissions", {
            "request_id": "s1", "work_id": "work1", "package_name": "final.zip",
            "content_digest": "a" * 64,
            "script_summary": {"synopsis": "摘要", "script_digest": "b" * 64},
            "material_manifest": [{"material_id": "m1", "name": "素材", "license": "cc0"}],
            "interaction_notes": {"summary": "说明"},
            "author_declaration": {"accepted": True, "signature_text": "签名"},
            "delivery_checklist": checklist()}, {"X-Actor-Id": "au"})
        self.assertEqual(201, status)
        self.assertEqual("submission", payload["resource_type"])

        # 缺凭据的提交返回 201 但进入待补证（另一作者的另一作品）
        route(self.service, "POST", "/organizations", {"request_id": "org-b",
              "organization_id": "ob", "name": "作者机构二"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/actors", {"request_id": "author2", "new_actor_id": "au2",
              "display_name": "作者二", "role": "author", "organization_id": "ob"},
              {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/works", {
            "request_id": "w2", "competition_id": "comp1", "work_id": "work2",
            "title": "作品二", "pseudonym": "化名二", "author_actor_id": "au2"},
            {"X-Actor-Id": "a1"})
        status, payload = route(self.service, "POST", "/submissions", {
            "request_id": "s2", "work_id": "work2", "package_name": "final.zip",
            "content_digest": "c" * 64,
            "script_summary": {"synopsis": "摘要", "script_digest": "d" * 64},
            "material_manifest": [{"material_id": "m1", "name": "素材", "license": "unknown"}],
            "interaction_notes": {"summary": "说明"},
            "author_declaration": {"accepted": True, "signature_text": "签名"},
            "delivery_checklist": checklist()}, {"X-Actor-Id": "au2"})
        self.assertEqual(201, status)

        # 公开查询无需操作者，且不暴露作者
        status, payload = route(self.service, "GET", "/works/work1/public", None)
        self.assertEqual(200, status)
        self.assertEqual("化名", payload["pseudonym"])
        self.assertNotIn("author", str(payload))

        # 审计查询无身份 -> 404（操作者不存在）；非授权角色 -> 403
        status, payload = route(self.service, "GET", "/works/work1/audit", None)
        self.assertEqual(404, status)
        status, payload = route(self.service, "GET", "/works/work1/audit", None,
                                {"X-Actor-Id": "au"})
        self.assertEqual(403, status)

    def test_idempotent_replay_returns_200(self):
        body = {"request_id": "c1", "competition_id": "comp1", "title": "展评",
                "deadline": "2026-09-10T00:00:00+00:00", "reviewers_per_work": 1,
                "rubric": RUBRIC}
        first, _ = route(self.service, "POST", "/competitions", dict(body), {"X-Actor-Id": "a1"})
        second, payload = route(self.service, "POST", "/competitions", dict(body),
                                {"X-Actor-Id": "a1"})
        self.assertEqual(201, first)
        self.assertEqual(200, second)
        self.assertTrue(payload["replayed"])


if __name__ == "__main__":
    unittest.main()
