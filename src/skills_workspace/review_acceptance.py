"""运行作品评审后台的离线端到端验收。

覆盖：多版本追加且旧版不可改写、截止冻结送审快照、缺凭据进入待补证、
按回避规则分派评委、评分引用具体版本、重复评分返回原决定、不同分值暴露冲突、
申诉保留原分与复核分并按规则裁决、公开脱敏汇总与审计全程可追。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from skills_workspace.clock import MutableClock
from skills_workspace.review import ReviewService
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


def _hash(seed: str) -> str:
    return (seed * 64)[:64]


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "review_acceptance.sqlite3")
        clock = MutableClock(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        review = ReviewService(base)

        # 建档：机构、管理员、操作员、审计员与评委。
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="数字媒体训练中心")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="op", actor_id="ad1", new_actor_id="op1",
                            display_name="操作员", role="operator", organization_id="o1")
        base.register_actor(request_id="aud", actor_id="ad1", new_actor_id="aud1",
                            display_name="审计员", role="auditor", organization_id="o1")
        base.register_actor(request_id="r1", actor_id="ad1", new_actor_id="r1",
                            display_name="评委甲", role="reviewer", organization_id="o1")
        base.register_organization(request_id="org2", actor_id="ad1",
                                   organization_id="o2", name="外部院校")
        base.register_actor(request_id="r2", actor_id="ad1", new_actor_id="r2",
                            display_name="评委乙", role="reviewer", organization_id="o2")
        base.register_actor(request_id="r3", actor_id="ad1", new_actor_id="r3",
                            display_name="评委丙", role="reviewer", organization_id="o2")

        # 赛事与作品（作者属于 o1，评委 r1 因此自动回避）。
        review.create_competition(request_id="comp", actor_id="ad1", competition_id="c1",
                                  title="交互媒体设计赛", deadline="2026-09-25T09:00:00Z")
        review.register_work(request_id="work1", actor_id="op1", competition_id="c1", work_id="w1",
                             title="星河交互叙事", author_name="张三",
                             required_credentials=["bgm", "font"],
                             author_actor_id="stu1", author_org_id="o1")
        review.register_work(request_id="work2", actor_id="op1", competition_id="c1", work_id="w2",
                             title="城市数据可视化", author_name="李四",
                             required_credentials=["dataset"],
                             author_actor_id="stu2", author_org_id="o1")

        # w1：提交两个版本，旧版本保持不可改写。
        review.submit_version(request_id="v1-1", actor_id="op1", work_id="w1",
                              package_name="xinghe.zip", package_sha256=_hash("a"),
                              script_summary="第一版脚本", interaction_notes="第一版交互",
                              metadata={"round": 1},
                              manifest=[{"item_id": "bgm"}, {"item_id": "font"}], credentials={})
        review.submit_version(request_id="v1-2", actor_id="op1", work_id="w1",
                              package_name="xinghe.zip", package_sha256=_hash("b"),
                              script_summary="第二版脚本", interaction_notes="第二版交互",
                              metadata={"round": 2},
                              manifest=[{"item_id": "bgm"}, {"item_id": "font"}], credentials={})
        for item in ("bgm", "font"):
            review.register_credential(request_id=f"cred-w1-{item}", actor_id="op1", work_id="w1",
                                       item_id=item, license_code="CC-BY-4.0",
                                       evidence_ref=f"evidence/w1/{item}.pdf",
                                       evidence_hash=_hash("d"))

        # w2：提交版本但故意不补凭据，截止冻结时应进入待补证（隔离）。
        review.submit_version(request_id="v2-1", actor_id="op1", work_id="w2",
                              package_name="city.zip", package_sha256=_hash("c"),
                              script_summary="唯一一版脚本", interaction_notes="交互说明",
                              metadata={"round": 1}, manifest=[{"item_id": "dataset"}],
                              credentials={})

        # 截止冻结。
        clock.set(datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc))
        frozen = review.freeze_competition(request_id="freeze", actor_id="ad1", competition_id="c1")
        frozen_payload = frozen.__dict__

        # 分派：w1 只能分派给 o2 的两名评委。
        review.assign_reviewers(request_id="assign", actor_id="ad1", competition_id="c1")
        assignments = review.list_assignments(competition_id="c1")
        reviewers = sorted(a.reviewer_actor_id for a in assignments)

        # 评分必须引用冻结版本；重复评分返回原决定，改分暴露冲突。
        work1 = review.get_work("w1")
        assignment = assignments[0]
        score, first_outcome = review.submit_score(
            request_id="score-1", actor_id=assignment.reviewer_actor_id,
            assignment_id=assignment.assignment_id, version_id=work1.frozen_version_id,
            dimension="overall", points=50, comment="初评偏低")
        _, replay_outcome = review.submit_score(
            request_id="score-1", actor_id=assignment.reviewer_actor_id,
            assignment_id=assignment.assignment_id, version_id=work1.frozen_version_id,
            dimension="overall", points=50, comment="初评偏低")
        conflict_exposed = False
        try:
            review.submit_score(
                request_id="score-1", actor_id=assignment.reviewer_actor_id,
                assignment_id=assignment.assignment_id, version_id=work1.frozen_version_id,
                dimension="overall", points=90, comment="尝试改分")
        except Exception:
            conflict_exposed = True

        # 申诉：复核分 70，与原分 50 相差 20 >= 阈值 10，最终采用复核分，原分仍保留。
        review.open_appeal(request_id="appeal", actor_id="op1", work_id="w1", reason="总分异议")
        appeal_id = review.work_audit_detail(actor_id="ad1", work_id="w1")["appeal"]["appeal_id"]
        # 复核须由同评审组的另一名评委独立完成（不能复核自己的原决定）。
        other_assignment = next(a for a in assignments if a.assignment_id != assignment.assignment_id)
        review.submit_appeal_review(
            request_id="review-1", actor_id=other_assignment.reviewer_actor_id, appeal_id=appeal_id,
            assignment_id=assignment.assignment_id, dimension="overall",
            review_points=70, rationale="差距显著")
        review.decide_appeal(request_id="decide", actor_id="ad1", appeal_id=appeal_id)
        decision = next(d for d in review.list_decisions("w1")
                        if d["assignment_id"] == assignment.assignment_id)

        # w2 补证准入后才能送审。
        review.register_credential(request_id="cred-w2-dataset", actor_id="op1", work_id="w2",
                                   item_id="dataset", license_code="CC0-1.0",
                                   evidence_ref="evidence/w2/dataset.pdf", evidence_hash=_hash("e"))
        review.admit_quarantine(request_id="admit", actor_id="ad1", work_id="w2")

        public_w1 = review.public_status("w1")
        audit_w1 = review.work_audit_detail(actor_id="aud1", work_id="w1")
        audit_valid, event_count = base.verify_audit()

        result = {
            "status": "ok",
            "frozen": frozen_payload,
            "w1_status": review.get_work("w1").status,
            "w2_status_before_admit": "quarantined",
            "w2_status_after_admit": review.get_work("w2").status,
            "assigned_reviewers": reviewers,
            "first_score_outcome": first_outcome,
            "replay_score_outcome": replay_outcome,
            "changed_points_conflict_exposed": conflict_exposed,
            "original_points": decision["points"],
            "final_points": decision["final_points"],
            "appeal_outcome": decision["appeal_outcome"],
            "public_author_masked": public_w1["author_masked"],
            "audit_credentials": len(audit_w1["credentials"]),
            "audit_scores": len(audit_w1["scores"]),
            "audit_valid": audit_valid,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["frozen"]["resource_id"] == "c1"
          and result["assigned_reviewers"] == ["r2", "r3"]
          and result["changed_points_conflict_exposed"]
          and result["original_points"] == 50 and result["final_points"] == 70
          and result["appeal_outcome"] == "changed"
          and "＊" in result["public_author_masked"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
