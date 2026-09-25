"""数字交互媒体作品评审后台。

本模块只登记作品的摘要与元数据（脚本摘要、素材清单、交互说明、
作者声明、交付清单、版权凭据摘要），不接触也不渲染任何媒体内容。

核心规则：
- 提交版本只追加：截止前可追加新版本，任何旧版本都没有改写路径；
- 截止时冻结送审快照，缺少必要版权凭据的作品只进入待补证状态；
- 按利益冲突、同组织回避和主动回避规则分派评委；
- 评分决定不可改写，且必须引用快照冻结的具体版本；
- 重复评分请求返回原决定，不同分值复用同一请求编号暴露冲突；
- 申诉期间原分与复核分都保留，按容差规则生成最终结果；
- 公开查询只展示脱敏汇总，审计查询可追到凭据、分派与每次决定。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .domain import (
    CREDENTIAL_REQUIRED_LICENSES,
    LICENSE_STATUSES,
    REQUIRED_CHECKLIST_ITEMS,
)
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import ScoreReceipt, WriteReceipt
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DIGEST = re.compile(r"^[0-9a-f]{16,128}$")
WRITE_ROLES = ("admin", "operator")


class ReviewService:
    """协调作品登记、只追加版本、冻结、分派、评分与申诉规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础校验
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 2000) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _digest(self, value: str, field: str) -> str:
        value = str(value or "").strip().lower()
        if not DIGEST.fullmatch(value):
            raise ValidationError(f"{field} 必须是十六进制内容摘要")
        return value

    def _deadline(self, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValidationError("deadline 必须是带时区的 ISO 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError("deadline 必须包含时区")
        return parsed.isoformat()

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_roles(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    def _competition(self, connection, competition_id: str):
        row = connection.execute(
            "SELECT * FROM competitions WHERE competition_id=?", (competition_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("赛事不存在")
        return row

    def _work(self, connection, work_id: str):
        row = connection.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return row

    def _rubric(self, competition) -> list[dict[str, Any]]:
        return json_loads(competition["rubric_json"])

    def _snapshot_for(self, connection, competition_id: str):
        row = connection.execute(
            "SELECT * FROM snapshots WHERE competition_id=?", (competition_id,)
        ).fetchone()
        if row is None:
            raise ConflictError("赛事尚未冻结送审快照")
        return row

    # ------------------------------------------------------------------
    # 赛事与作品登记
    # ------------------------------------------------------------------

    def register_competition(self, *, request_id: str, actor_id: str, competition_id: str,
                             title: str, deadline: str, reviewers_per_work: int = 3,
                             score_tolerance: float = 10.0,
                             rubric: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "competition_id": competition_id, "title": title,
                   "deadline": deadline, "reviewers_per_work": reviewers_per_work,
                   "score_tolerance": score_tolerance, "rubric": rubric}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, *WRITE_ROLES)
            competition_id = self._identifier(competition_id, "competition_id")
            title = self._text(title, "title")
            deadline_iso = self._deadline(deadline)
            if not isinstance(reviewers_per_work, int) or not 1 <= reviewers_per_work <= 9:
                raise ValidationError("reviewers_per_work 必须是 1 到 9 的整数")
            if not isinstance(score_tolerance, (int, float)) or score_tolerance < 0:
                raise ValidationError("score_tolerance 必须是非负数值")
            rubric = self._validate_rubric(rubric)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO competitions(competition_id,organization_id,title,deadline,"
                        "reviewers_per_work,score_tolerance,rubric_json,status)"
                        " VALUES(?,?,?,?,?,?,?,'open')",
                        (competition_id, actor["organization_id"], title, deadline_iso,
                         reviewers_per_work, float(score_tolerance), canonical_json(rubric)),
                    )
                except Exception as exc:
                    raise ConflictError("赛事编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="competition.registered",
                            resource_type="competition", resource_id=competition_id,
                            detail={"title": title, "deadline": deadline_iso,
                                    "reviewers_per_work": reviewers_per_work,
                                    "score_tolerance": float(score_tolerance),
                                    "dimensions": [d["dimension_id"] for d in rubric]})
                return "competition", competition_id, {"competition_id": competition_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_competition", payload=payload, create=create)

    def _validate_rubric(self, rubric: Any) -> list[dict[str, Any]]:
        if not isinstance(rubric, list) or not rubric:
            raise ValidationError("rubric 必须是非空维度数组")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rubric:
            if not isinstance(item, dict):
                raise ValidationError("评分维度必须是对象")
            dimension_id = self._identifier(item.get("dimension_id", ""), "dimension_id")
            if dimension_id in seen:
                raise ValidationError(f"评分维度重复: {dimension_id}")
            name = self._text(item.get("name", ""), "维度名称", 120)
            max_score = item.get("max_score")
            if not isinstance(max_score, (int, float)) or isinstance(max_score, bool) or max_score <= 0:
                raise ValidationError(f"维度 {dimension_id} 的 max_score 必须是正数")
            seen.add(dimension_id)
            normalized.append({"dimension_id": dimension_id, "name": name,
                               "max_score": float(max_score)})
        return normalized

    def register_work(self, *, request_id: str, actor_id: str, competition_id: str,
                      work_id: str, title: str, pseudonym: str,
                      author_actor_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "competition_id": competition_id, "work_id": work_id,
                   "title": title, "pseudonym": pseudonym, "author_actor_id": author_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            competition = self._competition(connection, competition_id)
            if actor["actor_id"] != author_actor_id:
                self._require_roles(actor, *WRITE_ROLES)
            author = self._actor(connection, author_actor_id)
            if author["role"] != "author":
                raise ValidationError("作者编号对应的操作者角色必须是 author")
            work_id = self._identifier(work_id, "work_id")
            title = self._text(title, "title")
            pseudonym = self._text(pseudonym, "pseudonym", 120)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO works(work_id,competition_id,author_actor_id,"
                        "author_organization_id,title,pseudonym,created_at) VALUES(?,?,?,?,?,?,?)",
                        (work_id, competition_id, author_actor_id, author["organization_id"],
                         title, pseudonym, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在或作者已在本赛事登记作品") from exc
                self._audit(connection, actor_id=actor_id, action="work.registered",
                            resource_type="work", resource_id=work_id,
                            detail={"competition_id": competition_id,
                                    "author_actor_id": author_actor_id,
                                    "author_organization_id": author["organization_id"],
                                    "title": title, "pseudonym": pseudonym})
                return "work", work_id, {"work_id": work_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_work", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 只追加的提交版本
    # ------------------------------------------------------------------

    def submit_version(self, *, request_id: str, actor_id: str, work_id: str,
                       package_name: str, content_digest: str,
                       script_summary: dict[str, Any],
                       material_manifest: list[dict[str, Any]],
                       interaction_notes: dict[str, Any],
                       author_declaration: dict[str, Any],
                       delivery_checklist: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "package_name": package_name,
                   "content_digest": content_digest, "script_summary": script_summary,
                   "material_manifest": material_manifest,
                   "interaction_notes": interaction_notes,
                   "author_declaration": author_declaration,
                   "delivery_checklist": delivery_checklist}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            work = self._work(connection, work_id)
            competition = self._competition(connection, competition_id=work["competition_id"])
            if actor["actor_id"] != work["author_actor_id"]:
                self._require_roles(actor, *WRITE_ROLES)
            if competition["status"] != "open":
                raise ConflictError("赛事已截止冻结，不能再追加版本")
            if self.clock.now() > datetime.fromisoformat(competition["deadline"]):
                raise ConflictError("已超过提交截止时间，不能再追加版本")
            package_name = self._text(package_name, "package_name", 260)
            content_digest = self._digest(content_digest, "content_digest")
            script_summary = self._validate_script_summary(script_summary)
            materials = self._validate_material_manifest(material_manifest)
            interaction_notes = self._validate_interaction_notes(interaction_notes)
            author_declaration = self._validate_declaration(author_declaration)
            delivery_checklist = self._validate_checklist(delivery_checklist)
            evidence_gaps = self._evidence_gaps(materials)
            evidence_status = "complete" if not evidence_gaps else "pending_evidence"

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT COALESCE(MAX(version_no),0) AS latest FROM submissions WHERE work_id=?",
                    (work_id,),
                ).fetchone()["latest"]
                version_no = latest + 1
                submission_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO submissions(submission_id,work_id,version_no,package_name,"
                        "content_digest,script_summary_json,material_manifest_json,"
                        "interaction_notes_json,author_declaration_json,delivery_checklist_json,"
                        "evidence_status,evidence_gap_json,submitted_by,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (submission_id, work_id, version_no, package_name, content_digest,
                         canonical_json(script_summary), canonical_json(materials),
                         canonical_json(interaction_notes), canonical_json(author_declaration),
                         canonical_json(delivery_checklist), evidence_status,
                         canonical_json(evidence_gaps), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("相同内容摘要的版本已经存在，旧版本不可改写") from exc
                self._audit(connection, actor_id=actor_id, action="submission.version_appended",
                            resource_type="submission", resource_id=submission_id,
                            detail={"work_id": work_id, "version_no": version_no,
                                    "package_name": package_name,
                                    "content_digest": content_digest,
                                    "evidence_status": evidence_status,
                                    "evidence_gaps": evidence_gaps,
                                    "material_count": len(materials)})
                response = {"submission_id": submission_id, "version_no": version_no,
                            "evidence_status": evidence_status, "evidence_gaps": evidence_gaps}
                return "submission", submission_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_version", payload=payload, create=create)

    def _validate_script_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("script_summary 必须是对象")
        synopsis = self._text(value.get("synopsis", ""), "script_summary.synopsis", 5000)
        script_digest = self._digest(value.get("script_digest", ""), "script_summary.script_digest")
        revision_note = str(value.get("revision_note", "") or "").strip()
        return {"synopsis": synopsis, "script_digest": script_digest,
                "revision_note": revision_note[:1000]}

    def _validate_material_manifest(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("material_manifest 必须是非空素材数组")
        materials: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("素材清单条目必须是对象")
            material_id = self._identifier(item.get("material_id", ""), "material_id")
            if material_id in seen:
                raise ValidationError(f"素材编号重复: {material_id}")
            name = self._text(item.get("name", ""), "素材名称", 200)
            license_status = str(item.get("license", "")).strip()
            if license_status not in LICENSE_STATUSES:
                raise ValidationError(f"素材 {material_id} 的许可状态不被认可")
            credential_digest = str(item.get("credential_digest", "") or "").strip().lower()
            if credential_digest and not DIGEST.fullmatch(credential_digest):
                raise ValidationError(f"素材 {material_id} 的凭据摘要格式无效")
            source = str(item.get("source", "") or "").strip()[:300]
            seen.add(material_id)
            materials.append({"material_id": material_id, "name": name,
                              "license": license_status,
                              "credential_digest": credential_digest, "source": source})
        return materials

    def _validate_interaction_notes(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError("interaction_notes 必须是对象")
        summary = self._text(value.get("summary", ""), "interaction_notes.summary", 5000)
        entry_points = value.get("entry_points", [])
        if not isinstance(entry_points, list):
            raise ValidationError("interaction_notes.entry_points 必须是数组")
        points = [str(item).strip()[:200] for item in entry_points[:50]]
        points = [item for item in points if item]
        return {"summary": summary, "entry_points": points}

    def _validate_declaration(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise ValidationError("作者声明必须显式 accepted=true")
        signature = self._text(value.get("signature_text", ""), "author_declaration.signature_text", 200)
        return {"accepted": True, "signature_text": signature}

    def _validate_checklist(self, value: Any) -> dict[str, bool]:
        if not isinstance(value, dict):
            raise ValidationError("delivery_checklist 必须是对象")
        checklist: dict[str, bool] = {}
        for item in REQUIRED_CHECKLIST_ITEMS:
            if value.get(item) is not True:
                raise ValidationError(f"交付清单缺少必要项确认: {item}")
            checklist[item] = True
        return checklist

    def _evidence_gaps(self, materials: list[dict[str, Any]]) -> list[dict[str, str]]:
        gaps: list[dict[str, str]] = []
        for material in materials:
            if material["license"] == "unknown":
                gaps.append({"material_id": material["material_id"],
                             "license": material["license"], "reason": "license_unknown"})
            elif material["license"] in CREDENTIAL_REQUIRED_LICENSES and not material["credential_digest"]:
                gaps.append({"material_id": material["material_id"],
                             "license": material["license"], "reason": "missing_credential_digest"})
        return gaps

    # ------------------------------------------------------------------
    # 截止冻结送审快照
    # ------------------------------------------------------------------

    def freeze_competition(self, *, request_id: str, actor_id: str,
                           competition_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "competition_id": competition_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, *WRITE_ROLES)
            competition = self._competition(connection, competition_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if competition["status"] == "frozen":
                    raise ConflictError("赛事已经冻结")
                if self.clock.now() < datetime.fromisoformat(competition["deadline"]):
                    raise ConflictError("尚未到截止时间，不能提前冻结")
                snapshot_id = uuid.uuid4().hex
                frozen_at = self._now()
                connection.execute(
                    "UPDATE competitions SET status='frozen', frozen_at=? WHERE competition_id=?",
                    (frozen_at, competition_id),
                )
                connection.execute(
                    "INSERT INTO snapshots(snapshot_id,competition_id,deadline,created_at) "
                    "VALUES(?,?,?,?)",
                    (snapshot_id, competition_id, competition["deadline"], frozen_at),
                )
                works = connection.execute(
                    "SELECT * FROM works WHERE competition_id=? ORDER BY work_id", (competition_id,)
                ).fetchall()
                eligible = 0
                pending = 0
                frozen_works: list[dict[str, Any]] = []
                for work in works:
                    latest = connection.execute(
                        "SELECT * FROM submissions WHERE work_id=? ORDER BY version_no DESC LIMIT 1",
                        (work["work_id"],),
                    ).fetchone()
                    if latest is None:
                        continue
                    connection.execute(
                        "INSERT INTO snapshot_versions(snapshot_id,work_id,submission_id,version_no,"
                        "content_digest,evidence_status) VALUES(?,?,?,?,?,?)",
                        (snapshot_id, work["work_id"], latest["submission_id"], latest["version_no"],
                         latest["content_digest"], latest["evidence_status"]),
                    )
                    if latest["evidence_status"] == "complete":
                        eligible += 1
                    else:
                        pending += 1
                    frozen_works.append({"work_id": work["work_id"],
                                         "version_no": latest["version_no"],
                                         "content_digest": latest["content_digest"],
                                         "evidence_status": latest["evidence_status"]})
                self._audit(connection, actor_id=actor_id, action="competition.frozen",
                            resource_type="snapshot", resource_id=snapshot_id,
                            detail={"competition_id": competition_id, "deadline": competition["deadline"],
                                    "frozen_at": frozen_at, "work_count": len(frozen_works),
                                    "eligible_count": eligible, "pending_evidence_count": pending,
                                    "works": frozen_works})
                response = {"snapshot_id": snapshot_id, "work_count": len(frozen_works),
                            "eligible_count": eligible, "pending_evidence_count": pending}
                return "snapshot", snapshot_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_competition", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 利益冲突登记、评委回避与分派
    # ------------------------------------------------------------------

    def register_conflict(self, *, request_id: str, actor_id: str, reviewer_id: str,
                          work_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "reviewer_id": reviewer_id, "work_id": work_id,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            reviewer = self._actor(connection, reviewer_id)
            if reviewer["role"] != "reviewer":
                raise ValidationError("被登记人不是评委")
            work = self._work(connection, work_id)
            if actor["actor_id"] != reviewer_id:
                self._require_roles(actor, *WRITE_ROLES)
            reason = self._text(reason, "reason", 500)
            conflict_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO reviewer_conflicts(conflict_id,reviewer_id,work_id,reason,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (conflict_id, reviewer_id, work_id, reason, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该评委对这一作品的利益冲突已经登记") from exc
                self._audit(connection, actor_id=actor_id, action="reviewer_conflict.registered",
                            resource_type="reviewer_conflict", resource_id=conflict_id,
                            detail={"competition_id": work["competition_id"],
                                    "reviewer_id": reviewer_id, "work_id": work_id, "reason": reason})
                return "reviewer_conflict", conflict_id, {"conflict_id": conflict_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_conflict", payload=payload, create=create)

    def assign_reviewers(self, *, request_id: str, actor_id: str,
                         competition_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "competition_id": competition_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, *WRITE_ROLES)
            competition = self._competition(connection, competition_id)
            snapshot = self._snapshot_for(connection, competition_id)
            snapshot_id = snapshot["snapshot_id"]
            required = competition["reviewers_per_work"]
            reviewers = connection.execute(
                "SELECT * FROM actors WHERE role='reviewer' AND active=1 ORDER BY actor_id"
            ).fetchall()

            def create() -> tuple[str, str, dict[str, Any]]:
                load = {row["actor_id"]: 0 for row in reviewers}
                for row in connection.execute(
                    "SELECT reviewer_id FROM review_assignments WHERE snapshot_id=? AND status='assigned'",
                    (snapshot_id,),
                ):
                    if row["reviewer_id"] in load:
                        load[row["reviewer_id"]] += 1
                targets = connection.execute(
                    "SELECT sv.*, w.author_organization_id FROM snapshot_versions sv"
                    " JOIN works w ON w.work_id=sv.work_id"
                    " WHERE sv.snapshot_id=? AND sv.evidence_status='complete' ORDER BY sv.work_id",
                    (snapshot_id,),
                ).fetchall()
                assignment_id = uuid.uuid4().hex
                assigned_map: dict[str, list[str]] = {}
                for target in targets:
                    work_id = target["work_id"]
                    existing = {row["reviewer_id"] for row in connection.execute(
                        "SELECT reviewer_id FROM review_assignments WHERE snapshot_id=? AND work_id=? "
                        "AND status='assigned'", (snapshot_id, work_id))}
                    blocked = {row["reviewer_id"] for row in connection.execute(
                        "SELECT reviewer_id FROM reviewer_conflicts WHERE work_id=?", (work_id,))}
                    blocked |= {row["reviewer_id"] for row in connection.execute(
                        "SELECT reviewer_id FROM review_assignments WHERE work_id=?", (work_id,))}
                    candidates = [
                        r for r in reviewers
                        if r["actor_id"] not in blocked
                        and r["organization_id"] != target["author_organization_id"]
                    ]
                    need = required - len(existing)
                    if len(candidates) < need:
                        raise ValidationError(
                            f"作品 {work_id} 可用评委不足，需 {need} 名，符合回避规则的仅 "
                            f"{len(candidates)} 名")
                    chosen = sorted(candidates, key=lambda r: (load[r["actor_id"]], r["actor_id"]))[:need]
                    assigned_map[work_id] = [r["actor_id"] for r in chosen]
                    next_slot_row = connection.execute(
                        "SELECT COALESCE(MAX(slot),0) AS slot FROM review_assignments"
                        " WHERE snapshot_id=? AND work_id=?", (snapshot_id, work_id)
                    ).fetchone()
                    slot = next_slot_row["slot"]
                    for reviewer in chosen:
                        slot += 1
                        connection.execute(
                            "INSERT INTO review_assignments(assignment_id,snapshot_id,work_id,"
                            "reviewer_id,slot,status,assigned_at) VALUES(?,?,?,?,?, 'assigned',?)",
                            (uuid.uuid4().hex, snapshot_id, work_id, reviewer["actor_id"],
                             slot, self._now()),
                        )
                        load[reviewer["actor_id"]] += 1
                for work_id, reviewer_ids in assigned_map.items():
                    self._audit(connection, actor_id=actor_id, action="reviewers.assigned",
                                resource_type="work", resource_id=work_id,
                                detail={"snapshot_id": snapshot_id, "reviewer_ids": reviewer_ids})
                return "review_assignment", assignment_id, {
                    "snapshot_id": snapshot_id,
                    "assignments": [{"work_id": work_id, "reviewer_ids": ids}
                                    for work_id, ids in sorted(assigned_map.items())]}

            return self._idempotent(connection, request_id=request_id,
                                    action="assign_reviewers", payload=payload, create=create)

    def recuse_assignment(self, *, request_id: str, actor_id: str,
                          assignment_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "assignment_id": assignment_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            assignment = connection.execute(
                "SELECT * FROM review_assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None:
                raise NotFoundError("分派记录不存在")
            if actor["actor_id"] != assignment["reviewer_id"]:
                self._require_roles(actor, *WRITE_ROLES)
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if assignment["status"] != "assigned":
                    raise ConflictError("该分派已经回避，不能重复操作")
                connection.execute(
                    "UPDATE review_assignments SET status='recused' WHERE assignment_id=?",
                    (assignment_id,),
                )
                self._audit(connection, actor_id=actor_id, action="assignment.recused",
                            resource_type="review_assignment", resource_id=assignment_id,
                            detail={"work_id": assignment["work_id"],
                                    "reviewer_id": assignment["reviewer_id"], "reason": reason})
                return "review_assignment", assignment_id, {"assignment_id": assignment_id,
                                                             "status": "recused"}

            return self._idempotent(connection, request_id=request_id,
                                    action="recuse_assignment", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 评分决定（不可改写，必须引用冻结版本）
    # ------------------------------------------------------------------

    def submit_score(self, *, request_id: str, actor_id: str, work_id: str,
                     submission_id: str, dimensions: dict[str, float],
                     rationale: str) -> ScoreReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "submission_id": submission_id,
                   "dimensions": dimensions, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "reviewer")
            work = self._work(connection, work_id)
            competition = self._competition(connection, work["competition_id"])
            snapshot = self._snapshot_for(connection, work["competition_id"])
            frozen = connection.execute(
                "SELECT * FROM snapshot_versions WHERE snapshot_id=? AND work_id=?",
                (snapshot["snapshot_id"], work_id),
            ).fetchone()
            if frozen is None:
                raise ConflictError("作品未进入送审快照")
            if frozen["evidence_status"] != "complete":
                raise ConflictError("作品处于待补证状态，不能评分")
            if submission_id != frozen["submission_id"]:
                raise ValidationError(
                    "评分必须引用冻结快照中的具体版本: "
                    f"v{frozen['version_no']} ({frozen['submission_id']})")
            assignment = connection.execute(
                "SELECT * FROM review_assignments WHERE snapshot_id=? AND work_id=? AND reviewer_id=?",
                (snapshot["snapshot_id"], work_id, actor_id),
            ).fetchone()
            if assignment is None or assignment["status"] != "assigned":
                raise PermissionDenied("当前评委没有这一作品的有效分派")
            scored = self._validate_dimensions(dimensions, self._rubric(competition))
            total = round(sum(scored.values()), 2)
            rationale = self._text(rationale, "rationale", 5000)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM score_decisions WHERE assignment_id=?", (assignment["assignment_id"],)
                ).fetchone()
                if existing is not None:
                    raise ConflictError("评分决定已存在且不可改写，重复请求应使用原 request_id")
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO score_decisions(decision_id,assignment_id,submission_id,"
                    "dimensions_json,total,rationale,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (decision_id, assignment["assignment_id"], submission_id,
                     canonical_json(scored), total, rationale, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="score.decided",
                            resource_type="score_decision", resource_id=decision_id,
                            detail={"work_id": work_id, "submission_id": submission_id,
                                    "version_no": frozen["version_no"],
                                    "content_digest": frozen["content_digest"],
                                    "assignment_id": assignment["assignment_id"],
                                    "dimensions": scored, "total": total})
                return "score_decision", decision_id, {"decision_id": decision_id, "total": total}

            return self._score_idempotent(connection, request_id=request_id, payload=payload,
                                          action="submit_score", create=create)

    def submit_appeal_review_score(self, *, request_id: str, actor_id: str, appeal_id: str,
                                   submission_id: str, dimensions: dict[str, float],
                                   rationale: str) -> ScoreReceipt:
        payload = {"actor_id": actor_id, "appeal_id": appeal_id,
                   "submission_id": submission_id, "dimensions": dimensions,
                   "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "reviewer")
            appeal = connection.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
            if appeal is None:
                raise NotFoundError("申诉不存在")
            if appeal["status"] != "open":
                raise ConflictError("申诉已经裁决，不能再提交复核分")
            work = self._work(connection, appeal["work_id"])
            competition = self._competition(connection, work["competition_id"])
            snapshot = self._snapshot_for(connection, work["competition_id"])
            frozen = connection.execute(
                "SELECT * FROM snapshot_versions WHERE snapshot_id=? AND work_id=?",
                (snapshot["snapshot_id"], work["work_id"]),
            ).fetchone()
            if submission_id != frozen["submission_id"]:
                raise ValidationError(
                    "复核必须引用冻结快照中的具体版本: "
                    f"v{frozen['version_no']} ({frozen['submission_id']})")
            if actor["organization_id"] == work["author_organization_id"]:
                raise PermissionDenied("复核评委与作者同组织，必须回避")
            conflict = connection.execute(
                "SELECT 1 FROM reviewer_conflicts WHERE reviewer_id=? AND work_id=?",
                (actor_id, work["work_id"]),
            ).fetchone()
            if conflict:
                raise PermissionDenied("复核评委对该作品登记过利益冲突")
            recused = connection.execute(
                "SELECT 1 FROM review_assignments WHERE reviewer_id=? AND work_id=? AND status='recused'",
                (actor_id, work["work_id"]),
            ).fetchone()
            if recused:
                raise PermissionDenied("复核评委已对该作品回避，不能再出具复核分")
            original_scorers = {row["decided_by"] for row in connection.execute(
                "SELECT sd.decided_by FROM score_decisions sd"
                " JOIN review_assignments ra ON ra.assignment_id=sd.assignment_id"
                " WHERE ra.work_id=?", (work["work_id"],))}
            if actor_id in original_scorers:
                raise PermissionDenied("原审评委不能再对同一作品出具复核分")
            scored = self._validate_dimensions(dimensions, self._rubric(competition))
            total = round(sum(scored.values()), 2)
            rationale = self._text(rationale, "rationale", 5000)

            def create() -> tuple[str, str, dict[str, Any]]:
                review_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO appeal_reviews(review_id,appeal_id,submission_id,reviewer_id,"
                        "dimensions_json,total,rationale,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (review_id, appeal_id, submission_id, actor_id, canonical_json(scored),
                         total, rationale, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该评委已对本申诉提交复核分，不可改写") from exc
                self._audit(connection, actor_id=actor_id, action="appeal.review_submitted",
                            resource_type="appeal_review", resource_id=review_id,
                            detail={"appeal_id": appeal_id, "work_id": work["work_id"],
                                    "submission_id": submission_id,
                                    "version_no": frozen["version_no"],
                                    "dimensions": scored, "total": total})
                return "appeal_review", review_id, {"review_id": review_id, "total": total}

            return self._score_idempotent(connection, request_id=request_id, payload=payload,
                                          action="submit_appeal_review", create=create)

    def _score_idempotent(self, connection, *, request_id: str, action: str,
                          payload: dict[str, Any],
                          create: Callable[[], tuple[str, str, dict[str, Any]]]) -> ScoreReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json_loads(row["response_json"])
            return ScoreReceipt(request_id, row["resource_id"], response["total"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return ScoreReceipt(request_id, resource_id, response["total"], False)

    def _validate_dimensions(self, dimensions: Any, rubric: list[dict[str, Any]]) -> dict[str, float]:
        if not isinstance(dimensions, dict) or not dimensions:
            raise ValidationError("dimensions 必须是非空对象")
        scored: dict[str, float] = {}
        maxima = {item["dimension_id"]: item["max_score"] for item in rubric}
        if set(dimensions) != set(maxima):
            raise ValidationError("评分维度必须与冻结评分表完全一致")
        for dimension_id, value in dimensions.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValidationError(f"维度 {dimension_id} 的分值必须是数字")
            value = float(value)
            if value < 0 or value > maxima[dimension_id]:
                raise ValidationError(
                    f"维度 {dimension_id} 分值超出范围 0-{maxima[dimension_id]}")
            scored[dimension_id] = value
        return scored

    # ------------------------------------------------------------------
    # 申诉与裁决
    # ------------------------------------------------------------------

    def file_appeal(self, *, request_id: str, actor_id: str, work_id: str,
                    reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            work = self._work(connection, work_id)
            if actor["actor_id"] != work["author_actor_id"]:
                self._require_roles(actor, *WRITE_ROLES)
            snapshot = self._snapshot_for(connection, work["competition_id"])
            decision_count = connection.execute(
                "SELECT COUNT(*) AS count FROM review_assignments ra"
                " JOIN score_decisions sd ON sd.assignment_id=ra.assignment_id"
                " WHERE ra.snapshot_id=? AND ra.work_id=?",
                (snapshot["snapshot_id"], work_id),
            ).fetchone()["count"]
            if decision_count == 0:
                raise ConflictError("作品尚无原审决定，不能提起申诉")
            open_appeal = connection.execute(
                "SELECT 1 FROM appeals WHERE work_id=? AND status='open'", (work_id,)
            ).fetchone()
            if open_appeal:
                raise ConflictError("该作品已有进行中的申诉")
            reason = self._text(reason, "reason", 5000)

            def create() -> tuple[str, str, dict[str, Any]]:
                appeal_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO appeals(appeal_id,work_id,snapshot_id,reason,status,filed_by,created_at)"
                    " VALUES(?,?,?,?,'open',?,?)",
                    (appeal_id, work_id, snapshot["snapshot_id"], reason, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="appeal.filed",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"work_id": work_id, "snapshot_id": snapshot["snapshot_id"],
                                    "original_decisions": decision_count})
                return "appeal", appeal_id, {"appeal_id": appeal_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="file_appeal", payload=payload, create=create)

    def rule_appeal(self, *, request_id: str, actor_id: str, appeal_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "appeal_id": appeal_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, *WRITE_ROLES)
            appeal = connection.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
            if appeal is None:
                raise NotFoundError("申诉不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if appeal["status"] != "open":
                    raise ConflictError("申诉已经裁决，决定不可改写")
                original_rows = connection.execute(
                    "SELECT sd.decision_id, sd.total FROM score_decisions sd"
                    " JOIN review_assignments ra ON ra.assignment_id=sd.assignment_id"
                    " WHERE ra.work_id=? ORDER BY sd.decision_id", (appeal["work_id"],),
                ).fetchall()
                review_rows = connection.execute(
                    "SELECT review_id, total FROM appeal_reviews WHERE appeal_id=? ORDER BY review_id",
                    (appeal_id,),
                ).fetchall()
                if not review_rows:
                    raise ConflictError("申诉还没有复核分，不能裁决")
                original_scores = [row["total"] for row in original_rows]
                review_scores = [row["total"] for row in review_rows]
                original_mean = round(sum(original_scores) / len(original_scores), 2)
                review_mean = round(sum(review_scores) / len(review_scores), 2)
                competition = self._competition(connection, self._work(connection, appeal["work_id"])["competition_id"])
                tolerance = float(competition["score_tolerance"])
                gap = round(review_mean - original_mean, 2)
                if abs(gap) <= tolerance:
                    final_total = original_mean
                    basis = "original_upheld"
                else:
                    final_total = review_mean
                    basis = "review_applied"
                ruled_at = self._now()
                connection.execute(
                    "UPDATE appeals SET status='ruled', final_total=?, final_basis=?, ruled_at=?"
                    " WHERE appeal_id=?",
                    (final_total, basis, ruled_at, appeal_id),
                )
                self._audit(connection, actor_id=actor_id, action="appeal.ruled",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"work_id": appeal["work_id"],
                                    "original_decision_ids": [r["decision_id"] for r in original_rows],
                                    "original_scores": original_scores,
                                    "original_mean": original_mean,
                                    "review_ids": [r["review_id"] for r in review_rows],
                                    "review_scores": review_scores,
                                    "review_mean": review_mean, "gap": gap,
                                    "tolerance": tolerance, "final_basis": basis,
                                    "final_total": final_total})
                return "appeal", appeal_id, {"appeal_id": appeal_id, "final_total": final_total,
                                             "final_basis": basis}

            return self._idempotent(connection, request_id=request_id,
                                    action="rule_appeal", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 公开脱敏查询
    # ------------------------------------------------------------------

    def public_work(self, work_id: str) -> dict[str, Any]:
        """对外只返回脱敏汇总：不暴露作者身份、评委身份或逐份评语。"""

        connection = self.database.connection
        work = self._work(connection, work_id)
        competition = self._competition(connection, work["competition_id"])
        result: dict[str, Any] = {
            "work_id": work_id,
            "pseudonym": work["pseudonym"],
            "competition_id": work["competition_id"],
            "status": "registered",
            "latest_version": None,
            "frozen_version": None,
            "scores": None,
            "appeal": None,
        }
        latest = connection.execute(
            "SELECT version_no, package_name, content_digest, evidence_status, created_at"
            " FROM submissions WHERE work_id=? ORDER BY version_no DESC LIMIT 1", (work_id,)
        ).fetchone()
        if latest:
            result["latest_version"] = {"version_no": latest["version_no"],
                                        "package_name": latest["package_name"],
                                        "content_digest": latest["content_digest"],
                                        "evidence_status": latest["evidence_status"],
                                        "created_at": latest["created_at"]}
            result["status"] = "accepting_versions" if competition["status"] == "open" else result["status"]
        frozen = connection.execute(
            "SELECT sv.* FROM snapshot_versions sv JOIN snapshots s ON s.snapshot_id=sv.snapshot_id"
            " WHERE s.competition_id=? AND sv.work_id=?",
            (work["competition_id"], work_id),
        ).fetchone()
        if frozen:
            result["frozen_version"] = {"version_no": frozen["version_no"],
                                        "content_digest": frozen["content_digest"],
                                        "evidence_status": frozen["evidence_status"]}
            if frozen["evidence_status"] != "complete":
                result["status"] = "pending_evidence"
                return result
            score_rows = connection.execute(
                "SELECT sd.total FROM score_decisions sd"
                " JOIN review_assignments ra ON ra.assignment_id=sd.assignment_id"
                " WHERE ra.work_id=?", (work_id,),
            ).fetchall()
            totals = [row["total"] for row in score_rows]
            if totals:
                result["scores"] = {"reviewer_count": len(totals),
                                    "average_total": round(sum(totals) / len(totals), 2)}
                result["status"] = "scored"
            else:
                result["status"] = "awaiting_review"
            appeal = connection.execute(
                "SELECT * FROM appeals WHERE work_id=? ORDER BY created_at DESC LIMIT 1", (work_id,)
            ).fetchone()
            if appeal:
                if appeal["status"] == "open":
                    result["status"] = "appeal_open"
                    result["appeal"] = {"status": "open", "original_scores": result["scores"]}
                else:
                    result["status"] = "final"
                    result["appeal"] = {"status": "ruled", "final_total": appeal["final_total"],
                                        "final_basis": appeal["final_basis"]}
        return result

    # ------------------------------------------------------------------
    # 审计全链路查询
    # ------------------------------------------------------------------

    def audit_work(self, *, actor_id: str, work_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "auditor")
            work = self._work(connection, work_id)
            competition = self._competition(connection, work["competition_id"])
            trace: dict[str, Any] = {
                "work": {"work_id": work["work_id"], "competition_id": work["competition_id"],
                         "title": work["title"], "pseudonym": work["pseudonym"],
                         "author_actor_id": work["author_actor_id"],
                         "author_organization_id": work["author_organization_id"],
                         "created_at": work["created_at"]},
                "competition": {"competition_id": competition["competition_id"],
                                "deadline": competition["deadline"],
                                "status": competition["status"],
                                "frozen_at": competition["frozen_at"],
                                "reviewers_per_work": competition["reviewers_per_work"],
                                "score_tolerance": competition["score_tolerance"],
                                "rubric": json_loads(competition["rubric_json"])},
                "submissions": [],
                "snapshot": None,
                "conflicts": [],
                "assignments": [],
                "decisions": [],
                "appeals": [],
                "audit_events": [],
            }
            resource_ids = {work_id, work["competition_id"]}
            for row in connection.execute(
                "SELECT * FROM submissions WHERE work_id=? ORDER BY version_no", (work_id,)
            ):
                resource_ids.add(row["submission_id"])
                trace["submissions"].append({
                    "submission_id": row["submission_id"], "version_no": row["version_no"],
                    "package_name": row["package_name"], "content_digest": row["content_digest"],
                    "script_summary": json_loads(row["script_summary_json"]),
                    "material_manifest": json_loads(row["material_manifest_json"]),
                    "interaction_notes": json_loads(row["interaction_notes_json"]),
                    "author_declaration": json_loads(row["author_declaration_json"]),
                    "delivery_checklist": json_loads(row["delivery_checklist_json"]),
                    "evidence_status": row["evidence_status"],
                    "evidence_gaps": json_loads(row["evidence_gap_json"]),
                    "submitted_by": row["submitted_by"], "created_at": row["created_at"]})
            snapshot = connection.execute(
                "SELECT s.*, sv.submission_id AS frozen_submission_id, sv.version_no AS frozen_version_no,"
                " sv.evidence_status AS frozen_evidence_status, sv.content_digest AS frozen_digest"
                " FROM snapshots s JOIN snapshot_versions sv ON sv.snapshot_id=s.snapshot_id"
                " WHERE s.competition_id=? AND sv.work_id=?",
                (work["competition_id"], work_id),
            ).fetchone()
            if snapshot:
                resource_ids.add(snapshot["snapshot_id"])
                trace["snapshot"] = {"snapshot_id": snapshot["snapshot_id"],
                                     "deadline": snapshot["deadline"],
                                     "created_at": snapshot["created_at"],
                                     "frozen_submission_id": snapshot["frozen_submission_id"],
                                     "frozen_version_no": snapshot["frozen_version_no"],
                                     "frozen_content_digest": snapshot["frozen_digest"],
                                     "evidence_status": snapshot["frozen_evidence_status"]}
            for row in connection.execute(
                "SELECT * FROM reviewer_conflicts WHERE work_id=? ORDER BY created_at", (work_id,)
            ):
                resource_ids.add(row["conflict_id"])
                trace["conflicts"].append(dict_from_row(row))
            for row in connection.execute(
                "SELECT ra.*, sd.decision_id FROM review_assignments ra"
                " LEFT JOIN score_decisions sd ON sd.assignment_id=ra.assignment_id"
                " WHERE ra.work_id=? ORDER BY ra.slot", (work_id,)
            ):
                resource_ids.add(row["assignment_id"])
                entry = dict_from_row(row)
                if row["decision_id"]:
                    resource_ids.add(row["decision_id"])
                trace["assignments"].append(entry)
            for row in connection.execute(
                "SELECT sd.*, ra.reviewer_id, ra.work_id, ra.snapshot_id FROM score_decisions sd"
                " JOIN review_assignments ra ON ra.assignment_id=sd.assignment_id"
                " WHERE ra.work_id=? ORDER BY sd.created_at", (work_id,)
            ):
                resource_ids.add(row["decision_id"])
                receipt = connection.execute(
                    "SELECT request_id FROM request_receipts WHERE resource_type='score_decision'"
                    " AND resource_id=?", (row["decision_id"],),
                ).fetchone()
                trace["decisions"].append({
                    "decision_id": row["decision_id"], "request_id": receipt["request_id"] if receipt else None,
                    "assignment_id": row["assignment_id"], "reviewer_id": row["reviewer_id"],
                    "submission_id": row["submission_id"],
                    "dimensions": json_loads(row["dimensions_json"]),
                    "total": row["total"], "rationale": row["rationale"],
                    "decided_by": row["decided_by"], "created_at": row["created_at"]})
            for appeal in connection.execute(
                "SELECT * FROM appeals WHERE work_id=? ORDER BY created_at", (work_id,)
            ):
                resource_ids.add(appeal["appeal_id"])
                entry = dict_from_row(appeal)
                entry["reviews"] = []
                for review in connection.execute(
                    "SELECT * FROM appeal_reviews WHERE appeal_id=? ORDER BY created_at",
                    (appeal["appeal_id"],),
                ):
                    resource_ids.add(review["review_id"])
                    entry["reviews"].append({"review_id": review["review_id"],
                                             "appeal_id": appeal["appeal_id"],
                                             "reviewer_id": review["reviewer_id"],
                                             "submission_id": review["submission_id"],
                                             "dimensions": json_loads(review["dimensions_json"]),
                                             "total": review["total"],
                                             "rationale": review["rationale"],
                                             "created_at": review["created_at"]})
                trace["appeals"].append(entry)
            placeholders = ",".join("?" for _ in resource_ids)
            trace["audit_events"] = [
                {"sequence": row["sequence"], "event_id": row["event_id"],
                 "actor_id": row["actor_id"], "action": row["action"],
                 "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                 "detail": json_loads(row["detail_json"]),
                 "previous_hash": row["previous_hash"], "event_hash": row["event_hash"],
                 "occurred_at": row["occurred_at"]}
                for row in connection.execute(
                    f"SELECT * FROM audit_events WHERE resource_id IN ({placeholders}) ORDER BY sequence",
                    tuple(resource_ids))]
            return trace


def json_loads(value: str) -> Any:
    import json

    return json.loads(value)


def dict_from_row(row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
