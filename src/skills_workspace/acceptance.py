"""运行基础服务与作品评审后台的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .review import ReviewService
from .service import DomainService
from .storage import Database


class MutableClock:
    """可推进的 UTC 时钟，用于跨截止时间的端到端验收。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


RUBRIC = [
    {"dimension_id": "script", "name": "脚本", "max_score": 100},
    {"dimension_id": "material", "name": "素材规范", "max_score": 100},
    {"dimension_id": "interaction", "name": "交互设计", "max_score": 100},
]


def _script(digest: str) -> dict:
    return {"synopsis": "交互叙事摘要", "script_digest": digest, "revision_note": "第二轮修改"}


def _notes() -> dict:
    return {"summary": "交互说明：三分支入口", "entry_points": ["start", "branch-a", "branch-b"]}


def _declaration() -> dict:
    return {"accepted": True, "signature_text": "作者签名：张"}


def _checklist() -> dict:
    return {"script": True, "material_manifest": True, "interaction_notes": True,
            "author_declaration": True, "evidence_package": True}


def _complete_materials() -> list[dict]:
    return [
        {"material_id": "bg", "name": "背景图", "license": "owned",
         "credential_digest": "", "source": "自制"},
        {"material_id": "music", "name": "授权配乐", "license": "licensed",
         "credential_digest": "a" * 64, "source": "授权音乐库"},
    ]


def _pending_materials() -> list[dict]:
    return [
        {"material_id": "clip", "name": "来源不明视频", "license": "unknown",
         "credential_digest": "", "source": ""},
    ]


def run() -> dict[str, object]:
    """执行完整登记链、评审链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = MutableClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        review = ReviewService(database, clock)

        # ---- 基础登记链（保持原有验收） ----
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范训练机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="训练负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        records = service.list_domain_data("site-001")

        # ---- 作品评审后台链 ----
        service.register_organization(request_id="org-rev", actor_id="admin-001",
                                      organization_id="org-rev", name="评委机构")
        service.register_organization(request_id="org-author", actor_id="admin-001",
                                      organization_id="org-author", name="作者机构")
        service.register_actor(request_id="auditor-1", actor_id="admin-001", new_actor_id="auditor-001",
                               display_name="审计员", role="auditor", organization_id="org-001")
        for index in range(1, 5):
            service.register_actor(request_id=f"reviewer-{index}", actor_id="admin-001",
                                   new_actor_id=f"rv-{index}", display_name=f"评委{index}",
                                   role="reviewer", organization_id="org-rev")
        service.register_actor(request_id="author-1", actor_id="admin-001", new_actor_id="author-001",
                               display_name="参赛作者", role="author", organization_id="org-author")
        service.register_actor(request_id="author-2", actor_id="admin-001", new_actor_id="author-002",
                               display_name="待补证作者", role="author", organization_id="org-author")

        review.register_competition(
            request_id="comp-1", actor_id="operator-001", competition_id="comp-001",
            title="数字交互媒体设计训练展评", deadline="2026-09-20T00:00:00+00:00",
            reviewers_per_work=2, score_tolerance=10.0, rubric=RUBRIC)
        review.register_work(request_id="work-1", actor_id="operator-001", competition_id="comp-001",
                             work_id="work-001", title="同源压缩包之惑", pseudonym="化名星河",
                             author_actor_id="author-001")
        review.register_work(request_id="work-2", actor_id="operator-001", competition_id="comp-001",
                             work_id="work-002", title="待补证作品", pseudonym="化名迷雾",
                             author_actor_id="author-002")

        # 同名压缩包两轮提交：v1 与 v2 内容摘要不同，均保留，旧版不改写。
        review.submit_version(request_id="sub-1", actor_id="author-001", work_id="work-001",
                              package_name="final.zip", content_digest="1" * 64,
                              script_summary=_script("2" * 64),
                              material_manifest=_complete_materials(),
                              interaction_notes=_notes(), author_declaration=_declaration(),
                              delivery_checklist=_checklist())
        v2 = review.submit_version(request_id="sub-2", actor_id="author-001", work_id="work-001",
                                   package_name="final.zip", content_digest="3" * 64,
                                   script_summary=_script("4" * 64),
                                   material_manifest=_complete_materials(),
                                   interaction_notes=_notes(), author_declaration=_declaration(),
                                   delivery_checklist=_checklist())
        pending = review.submit_version(request_id="sub-3", actor_id="author-002", work_id="work-002",
                                        package_name="final.zip", content_digest="5" * 64,
                                        script_summary=_script("6" * 64),
                                        material_manifest=_pending_materials(),
                                        interaction_notes=_notes(), author_declaration=_declaration(),
                                        delivery_checklist=_checklist())

        clock.advance(days=20)
        frozen = review.freeze_competition(request_id="freeze-1", actor_id="operator-001",
                                           competition_id="comp-001")
        review.register_conflict(request_id="conflict-1", actor_id="rv-1", reviewer_id="rv-1",
                                 work_id="work-001", reason="曾指导该作品")
        assigned = review.assign_reviewers(request_id="assign-1", actor_id="operator-001",
                                           competition_id="comp-001")

        frozen_submission = database.connection.execute(
            "SELECT submission_id FROM snapshot_versions sv JOIN snapshots s ON s.snapshot_id=sv.snapshot_id"
            " WHERE s.competition_id='comp-001' AND sv.work_id='work-001'").fetchone()["submission_id"]
        assigned_reviewers = database.connection.execute(
            "SELECT reviewer_id FROM review_assignments WHERE work_id='work-001' ORDER BY slot").fetchall()
        reviewer_ids = [row["reviewer_id"] for row in assigned_reviewers]

        score_1 = review.submit_score(request_id="score-1", actor_id=reviewer_ids[0], work_id="work-001",
                                      submission_id=frozen_submission,
                                      dimensions={"script": 80, "material": 80, "interaction": 80},
                                      rationale="结构完整")
        score_replay = review.submit_score(request_id="score-1", actor_id=reviewer_ids[0],
                                           work_id="work-001", submission_id=frozen_submission,
                                           dimensions={"script": 80, "material": 80, "interaction": 80},
                                           rationale="结构完整")
        review.submit_score(request_id="score-2", actor_id=reviewer_ids[1], work_id="work-001",
                            submission_id=frozen_submission,
                            dimensions={"script": 80, "material": 80, "interaction": 80},
                            rationale="素材规范")

        review.file_appeal(request_id="appeal-1", actor_id="author-001", work_id="work-001",
                           reason="认为交互维度被低估")
        appeal_id = database.connection.execute(
            "SELECT appeal_id FROM appeals WHERE work_id='work-001'").fetchone()["appeal_id"]
        # 复核人：非原审（rv-2/rv-3 原审）、无冲突（rv-1 冲突），故为 rv-4。
        appeal_reviewer = next(rid for rid in ("rv-1", "rv-2", "rv-3", "rv-4")
                               if rid not in reviewer_ids and rid != "rv-1")
        review.submit_appeal_review_score(
            request_id="appeal-review-1", actor_id=appeal_reviewer, appeal_id=appeal_id,
            submission_id=frozen_submission,
            dimensions={"script": 85, "material": 85, "interaction": 90},
            rationale="交互设计确有亮点")
        ruled = review.rule_appeal(request_id="rule-1", actor_id="operator-001", appeal_id=appeal_id)

        public_complete = review.public_work("work-001")
        public_pending = review.public_work("work-002")
        public_text = json.dumps(public_complete, ensure_ascii=False)
        trace = review.audit_work(actor_id="auditor-001", work_id="work-001")
        valid, event_count = service.verify_audit()

        result = {
            "status": "ok",
            # 基础链
            "records": len(records),
            "audit_events": event_count,
            "audit_valid": valid,
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            # 评审链
            "frozen_version_is_v2": frozen_submission == v2.resource_id,
            "pending_evidence_status": _pending_status(database),
            "snapshot_eligible": _response(frozen, database)["eligible_count"] == 1,
            "conflicted_reviewer_excluded": "rv-1" not in reviewer_ids,
            "score_replayed": score_replay.replayed and score_replay.decision_id == score_1.decision_id,
            "appeal_final_total": ruled.resource_id and database.connection.execute(
                "SELECT final_total FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()["final_total"],
            "appeal_basis": database.connection.execute(
                "SELECT final_basis FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()["final_basis"],
            "public_is_masked": ("author-001" not in public_text and "rv-" not in public_text
                                 and public_complete["pseudonym"] == "化名星河"
                                 and public_complete["scores"]["average_total"] == 240.0),
            "public_pending_status": public_pending["status"],
            "audit_trace_has_credentials": trace["submissions"][1]["material_manifest"][1]
                                           ["credential_digest"] == "a" * 64,
            "audit_trace_decisions": len(trace["decisions"]),
        }
        database.close()
        return result


def _pending_status(database: Database) -> str:
    return database.connection.execute(
        "SELECT evidence_status FROM submissions WHERE work_id='work-002'").fetchone()["evidence_status"]


def _response(receipt, database: Database) -> dict:
    row = database.connection.execute(
        "SELECT response_json FROM request_receipts WHERE request_id=?", (receipt.request_id,)
    ).fetchone()
    return json.loads(row["response_json"])


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ("status", "audit_valid", "second_replayed", "frozen_version_is_v2",
                "snapshot_eligible", "conflicted_reviewer_excluded", "score_replayed",
                "public_is_masked", "audit_trace_has_credentials")
    ok = result["status"] == "ok" and result["audit_valid"] and all(result[key] for key in required)
    ok = ok and result["appeal_basis"] == "review_applied" and result["appeal_final_total"] == 260.0
    ok = ok and result["public_pending_status"] == "pending_evidence"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
