"""实现作品评审后台：版本登记、截止冻结、回避分派、评分与申诉。

本模块只登记作品的摘要、元数据、素材许可凭据与交付清单，
不负责渲染或存储任何媒体内容。所有写入复用基础服务的操作者权限、
请求幂等、SQLite 事务与哈希串联审计。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    Actor,
    AppealDecision,
    Assignment,
    ScoreRecord,
    WorkRecord,
    WorkVersion,
    WriteReceipt,
)
from .service import DomainService


# 评分维度：脚本、素材许可、交互设计、交付完整性、综合。
SCORE_DIMENSIONS = frozenset({
    "script",
    "material_licensing",
    "interaction_design",
    "delivery_completeness",
    "overall",
})

# 每件作品计划分派的评委数量。
DEFAULT_REVIEW_COUNT = 2

# 复核分与原分相差达到该阈值（含）时，以复核分为最终结果，否则维持原分。
APPEAL_CHANGE_THRESHOLD = 10

HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReviewService:
    """在基础领域服务之上编排评审规则。"""

    def __init__(self, base: DomainService) -> None:
        self.base = base
        self.database = base.database

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        return self.base._now()

    def _text(self, value: str, field: str, limit: int = 2000) -> str:
        return self.base._text(value, field, limit)

    def _identifier(self, value: str, field: str) -> str:
        return self.base._identifier(value, field)

    def _actor(self, connection, actor_id: str) -> Actor:
        return self.base._actor(connection, actor_id)

    def _require(self, actor: Actor, *roles: str) -> None:
        self.base._require(actor, *roles)

    def _idempotent(self, connection, **kwargs):
        return self.base._idempotent(connection, **kwargs)

    def _parse_deadline(self, value: str) -> str:
        text = self._text(value, "deadline", 64).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("deadline 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError("deadline 必须包含时区")
        # 归一化为与 _now() 一致的 UTC 文本，保证截止比较可按字符串进行。
        normalized = parsed.astimezone(timezone.utc).replace(microsecond=0)
        return normalized.isoformat().replace("+00:00", "Z")

    def _deadline_passed(self, deadline_text: str) -> bool:
        """判断当前时间是否已经严格超过截止时间。"""

        now = self.base.clock.now().astimezone(timezone.utc).replace(microsecond=0)
        deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        return now > deadline

    def _before_deadline(self, deadline_text: str) -> bool:
        """判断当前时间是否尚在截止时间之前（未到点不能冻结）。"""

        now = self.base.clock.now().astimezone(timezone.utc).replace(microsecond=0)
        deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        return now < deadline

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

    def _required(self, work_row) -> tuple[str, ...]:
        return tuple(json.loads(work_row["required_credentials_json"]))

    def _credential_evidence(self, connection, work_id: str) -> dict[str, str]:
        """汇总一件作品已登记的必要凭据：条目 -> 最新凭据编号。"""

        evidence: dict[str, str] = {}
        rows = connection.execute(
            "SELECT item_id, credential_id FROM credential_records WHERE work_id=? ORDER BY created_at, credential_id",
            (work_id,),
        ).fetchall()
        for row in rows:
            evidence[row["item_id"]] = row["credential_id"]
        return evidence

    def _missing_credentials(self, connection, work_row) -> list[str]:
        evidence = self._credential_evidence(connection, work_row["work_id"])
        return [item for item in self._required(work_row) if item not in evidence]

    def _conflicted(self, connection, reviewer: Actor, work_row) -> bool:
        """按利益冲突与回避规则判断评委是否需要回避该作品。"""

        if work_row["author_actor_id"] and reviewer.actor_id == work_row["author_actor_id"]:
            return True
        if work_row["author_org_id"] and reviewer.organization_id == work_row["author_org_id"]:
            return True
        keys = {work_row["work_id"]}
        if work_row["author_actor_id"]:
            keys.add(work_row["author_actor_id"])
        if work_row["author_org_id"]:
            keys.add(work_row["author_org_id"])
        placeholders = ",".join("?" for _ in keys)
        row = connection.execute(
            f"SELECT 1 FROM reviewer_conflicts WHERE reviewer_actor_id=? AND conflict_key IN ({placeholders}) LIMIT 1",
            (reviewer.actor_id, *sorted(keys)),
        ).fetchone()
        return row is not None

    def _eligible_reviewers(self, connection, work_row) -> list[Actor]:
        """返回对作品无需回避、且尚未分派（含已回避）的活跃评委，按编号排序。"""

        actors = [
            Actor(r["actor_id"], r["display_name"], r["role"], r["organization_id"], bool(r["active"]))
            for r in connection.execute(
                "SELECT * FROM actors WHERE role='reviewer' AND active=1 ORDER BY actor_id"
            )
        ]
        used = {
            r["reviewer_actor_id"]
            for r in connection.execute(
                "SELECT reviewer_actor_id FROM assignments WHERE work_id=?", (work_row["work_id"],)
            )
        }
        return [a for a in actors if a.actor_id not in used and not self._conflicted(connection, a, work_row)]

    def _version(self, connection, version_id: str):
        row = connection.execute(
            "SELECT * FROM work_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("版本不存在")
        return row

    def _to_version(self, row) -> WorkVersion:
        return WorkVersion(
            row["version_id"], row["work_id"], row["sequence_no"], row["package_name"],
            row["package_sha256"], row["script_summary"], row["interaction_notes"],
            json.loads(row["metadata_json"]), json.loads(row["manifest_json"]),
            row["manifest_hash"], json.loads(row["credentials_json"]),
            row["created_by"], row["created_at"],
        )

    # ---------------------------------------------------------- 赛事与作品

    def create_competition(self, *, request_id: str, actor_id: str, competition_id: str,
                           title: str, deadline: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "competition_id": competition_id,
                   "title": title, "deadline": deadline}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            competition_id = self._identifier(competition_id, "competition_id")
            title = self._text(title, "title")
            deadline = self._parse_deadline(deadline)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO competitions(competition_id,title,deadline,status) VALUES(?,?,?,'open')",
                        (competition_id, title, deadline),
                    )
                except Exception as exc:
                    raise ConflictError("赛事编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="competition.created",
                             resource_type="competition", resource_id=competition_id,
                             detail={"title": title, "deadline": deadline}, occurred_at=self._now())
                return "competition", competition_id, {"competition_id": competition_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_competition", payload=payload, create=create)

    def register_work(self, *, request_id: str, actor_id: str, competition_id: str, work_id: str,
                      title: str, author_name: str, required_credentials: list[str],
                      author_actor_id: str | None = None,
                      author_org_id: str | None = None) -> WriteReceipt:
        if not isinstance(required_credentials, list) or not required_credentials:
            raise ValidationError("required_credentials 必须是非空数组")
        required = [self._identifier(item, "required_credentials[]") for item in required_credentials]
        if len(set(required)) != len(required):
            raise ValidationError("必要版权凭据条目不能重复")
        payload = {"actor_id": actor_id, "competition_id": competition_id, "work_id": work_id,
                   "title": title, "author_name": author_name, "required_credentials": required,
                   "author_actor_id": author_actor_id, "author_org_id": author_org_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            competition = self._competition(connection, competition_id)
            work_id = self._identifier(work_id, "work_id")
            title = self._text(title, "title")
            author_name = self._text(author_name, "author_name", 100)
            if author_actor_id:
                author_actor_id = self._identifier(author_actor_id, "author_actor_id")
            if author_org_id:
                author_org_id = self._identifier(author_org_id, "author_org_id")
            # 登记时尚无任何凭据，作品直接进入待补证状态。
            status = "pending_evidence"

            def create() -> tuple[str, str, dict[str, Any]]:
                if competition["status"] != "open":
                    raise ConflictError("赛事已经冻结，不能再登记作品")
                try:
                    connection.execute(
                        "INSERT INTO works(work_id,competition_id,title,author_name,author_actor_id,"
                        "author_org_id,status,required_credentials_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (work_id, competition_id, title, author_name, author_actor_id, author_org_id,
                         status, canonical_json(required), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="work.registered",
                             resource_type="work", resource_id=work_id,
                             detail={"work_id": work_id, "competition_id": competition_id,
                                     "title": title, "author_name": author_name,
                                     "required_credentials": required, "status": status},
                             occurred_at=self._now())
                return "work", work_id, {"work_id": work_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_work", payload=payload, create=create)

    def submit_version(self, *, request_id: str, actor_id: str, work_id: str, package_name: str,
                       package_sha256: str, script_summary: str, interaction_notes: str,
                       metadata: dict[str, Any], manifest: list[dict[str, Any]],
                       credentials: dict[str, str]) -> WriteReceipt:
        if not isinstance(metadata, dict) or not metadata:
            raise ValidationError("metadata 必须是非空对象")
        if not isinstance(manifest, list) or not manifest:
            raise ValidationError("manifest 必须是非空数组（交付清单）")
        if not isinstance(credentials, dict):
            raise ValidationError("credentials 必须是对象（素材条目 -> 凭据编号）")
        for index, item in enumerate(manifest):
            if not isinstance(item, dict) or "item_id" not in item:
                raise ValidationError(f"manifest[{index}] 必须包含 item_id")
        payload = {"actor_id": actor_id, "work_id": work_id, "package_name": package_name,
                   "package_sha256": package_sha256, "script_summary": script_summary,
                   "interaction_notes": interaction_notes, "metadata": metadata,
                   "manifest": manifest, "credentials": credentials}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            work = self._work(connection, work_id)
            package_name = self._text(package_name, "package_name", 300)
            package_sha256 = str(package_sha256).strip().lower()
            if not HEX_SHA256.fullmatch(package_sha256):
                raise ValidationError("package_sha256 必须是 64 位十六进制摘要")
            script_summary = self._text(script_summary, "script_summary", 5000)
            interaction_notes = self._text(interaction_notes, "interaction_notes", 5000)
            manifest_hash = digest(manifest)

            def create() -> tuple[str, str, dict[str, Any]]:
                competition = self._competition(connection, work["competition_id"])
                if competition["status"] != "open" or work["status"] in ("frozen", "quarantined"):
                    raise ConflictError("送审已经冻结，旧版本不可改写且不能追加新版本")
                if self._deadline_passed(competition["deadline"]):
                    raise ConflictError("已超过提交截止时间")
                # 同名压缩包允许内容不同（按摘要登记为新版本）；完全相同的内容不得重复追加。
                duplicate = connection.execute(
                    "SELECT sequence_no FROM work_versions WHERE work_id=? AND package_sha256=? LIMIT 1",
                    (work_id, package_sha256),
                ).fetchone()
                if duplicate:
                    raise ConflictError(
                        f"该压缩包内容已登记为第 {duplicate['sequence_no']} 版，禁止重复追加新版本")
                last = connection.execute(
                    "SELECT MAX(sequence_no) AS seq FROM work_versions WHERE work_id=?", (work_id,)
                ).fetchone()
                sequence_no = (last["seq"] or 0) + 1
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO work_versions(version_id,work_id,sequence_no,package_name,package_sha256,"
                    "script_summary,interaction_notes,metadata_json,manifest_json,manifest_hash,"
                    "credentials_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, work_id, sequence_no, package_name, package_sha256, script_summary,
                     interaction_notes, canonical_json(metadata), canonical_json(manifest),
                     manifest_hash, canonical_json(credentials), actor_id, self._now()),
                )
                missing = self._missing_credentials(connection, work)
                new_status = "submittable" if not missing else "pending_evidence"
                if work["status"] != new_status:
                    connection.execute(
                        "UPDATE works SET status=? WHERE work_id=?", (new_status, work_id)
                    )
                append_event(connection, actor_id=actor_id, action="version.submitted",
                             resource_type="work_version", resource_id=version_id,
                             detail={"work_id": work_id, "sequence_no": sequence_no,
                                     "package_name": package_name, "package_sha256": package_sha256,
                                     "manifest_hash": manifest_hash, "missing_credentials": missing,
                                     "status": new_status},
                             occurred_at=self._now())
                return "work_version", version_id, {"version_id": version_id, "sequence_no": sequence_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_version", payload=payload, create=create)

    def register_credential(self, *, request_id: str, actor_id: str, work_id: str, item_id: str,
                            license_code: str, evidence_ref: str, evidence_hash: str | None = None,
                            version_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "item_id": item_id,
                   "license_code": license_code, "evidence_ref": evidence_ref,
                   "evidence_hash": evidence_hash, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            work = self._work(connection, work_id)
            item_id = self._identifier(item_id, "item_id")
            if item_id not in self._required(work):
                raise ValidationError("该素材条目不在作品的必要版权凭据清单内")
            license_code = self._text(license_code, "license_code", 100)
            evidence_ref = self._text(evidence_ref, "evidence_ref", 500)
            if evidence_hash:
                evidence_hash = str(evidence_hash).strip().lower()
                if not HEX_SHA256.fullmatch(evidence_hash):
                    raise ValidationError("evidence_hash 必须是 64 位十六进制摘要")
            if version_id:
                version_row = self._version(connection, version_id)
                if version_row["work_id"] != work_id:
                    raise ValidationError("凭据引用的版本不属于该作品")

            def create() -> tuple[str, str, dict[str, Any]]:
                if work["status"] == "frozen":
                    raise ConflictError("作品已完成送审，版权凭据不能再追加")
                already = connection.execute(
                    "SELECT credential_id FROM credential_records WHERE work_id=? AND item_id=?",
                    (work_id, item_id),
                ).fetchone()
                if already is not None:
                    raise ConflictError("该素材条目的版权凭据已经登记，旧凭据不可改写")
                credential_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO credential_records(credential_id,work_id,version_id,item_id,"
                    "license_code,evidence_ref,evidence_hash,recorded_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (credential_id, work_id, version_id, item_id, license_code, evidence_ref,
                     evidence_hash, actor_id, self._now()),
                )
                missing = self._missing_credentials(connection, work)
                new_status = work["status"]
                if work["status"] == "pending_evidence" and not missing:
                    new_status = "submittable"
                    connection.execute("UPDATE works SET status='submittable' WHERE work_id=?", (work_id,))
                append_event(connection, actor_id=actor_id, action="credential.registered",
                             resource_type="credential", resource_id=credential_id,
                             detail={"work_id": work_id, "item_id": item_id,
                                     "license_code": license_code, "evidence_ref": evidence_ref,
                                     "evidence_hash": evidence_hash, "version_id": version_id,
                                     "missing_credentials": missing, "status": new_status},
                             occurred_at=self._now())
                return "credential", credential_id, {"credential_id": credential_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_credential", payload=payload, create=create)

    # -------------------------------------------------------------- 冻结

    def freeze_competition(self, *, request_id: str, actor_id: str, competition_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "competition_id": competition_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            competition_id = self._identifier(competition_id, "competition_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                competition = self._competition(connection, competition_id)
                if competition["status"] != "open":
                    raise ConflictError("赛事已经冻结")
                if self._before_deadline(competition["deadline"]):
                    raise ConflictError("尚未到达截止时间，不能提前冻结送审快照")
                works = connection.execute(
                    "SELECT * FROM works WHERE competition_id=? ORDER BY work_id", (competition_id,)
                ).fetchall()
                if not works:
                    raise ConflictError("赛事下没有作品，无法冻结")
                no_version = [w["work_id"] for w in works if connection.execute(
                    "SELECT 1 FROM work_versions WHERE work_id=? LIMIT 1", (w["work_id"],)
                ).fetchone() is None]
                if no_version:
                    raise ConflictError(f"作品尚无任何版本，无法冻结: {','.join(no_version)}")
                frozen_at = self._now()
                frozen_version_ids: list[str] = []
                quarantined: list[str] = []
                for work in works:
                    latest = connection.execute(
                        "SELECT * FROM work_versions WHERE work_id=? ORDER BY sequence_no DESC LIMIT 1",
                        (work["work_id"],),
                    ).fetchone()
                    missing = self._missing_credentials(connection, work)
                    if missing:
                        status = "quarantined"
                        quarantined.append(work["work_id"])
                    else:
                        status = "frozen"
                        frozen_version_ids.append(latest["version_id"])
                    connection.execute(
                        "INSERT INTO frozen_snapshots(work_id,version_id,package_name,package_sha256,"
                        "script_summary,interaction_notes,metadata_json,manifest_json,manifest_hash,"
                        "credentials_json,frozen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (work["work_id"], latest["version_id"], latest["package_name"],
                         latest["package_sha256"], latest["script_summary"], latest["interaction_notes"],
                         latest["metadata_json"], latest["manifest_json"], latest["manifest_hash"],
                         latest["credentials_json"], frozen_at),
                    )
                    connection.execute(
                        "UPDATE works SET status=?, frozen_version_id=? WHERE work_id=?",
                        (status, latest["version_id"], work["work_id"]),
                    )
                    append_event(connection, actor_id=actor_id,
                                 action="snapshot.frozen" if status == "frozen" else "work.quarantined",
                                 resource_type="frozen_snapshot", resource_id=work["work_id"],
                                 detail={"work_id": work["work_id"],
                                         "version_id": latest["version_id"],
                                         "sequence_no": latest["sequence_no"],
                                         "package_name": latest["package_name"],
                                         "package_sha256": latest["package_sha256"],
                                         "manifest_hash": latest["manifest_hash"],
                                         "missing_credentials": missing, "status": status},
                                 occurred_at=frozen_at)
                connection.execute(
                    "UPDATE competitions SET status='frozen', frozen_at=? WHERE competition_id=?",
                    (frozen_at, competition_id),
                )
                append_event(connection, actor_id=actor_id, action="competition.frozen",
                             resource_type="competition", resource_id=competition_id,
                             detail={"competition_id": competition_id, "frozen_at": frozen_at,
                                     "frozen_works": frozen_version_ids, "quarantined_works": quarantined},
                             occurred_at=frozen_at)
                return "competition", competition_id, {
                    "competition_id": competition_id, "frozen": len(frozen_version_ids),
                    "quarantined": len(quarantined)}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_competition", payload=payload, create=create)

    def admit_quarantine(self, *, request_id: str, actor_id: str, work_id: str) -> WriteReceipt:
        """待补证作品在截止后补齐凭据，经核验后准入送审。"""

        payload = {"actor_id": actor_id, "work_id": work_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            work = self._work(connection, work_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if work["status"] != "quarantined":
                    raise ConflictError("只有待补证（隔离）作品可以补证准入")
                missing = self._missing_credentials(connection, work)
                if missing:
                    raise ConflictError(f"仍缺少必要版权凭据: {','.join(missing)}")
                connection.execute("UPDATE works SET status='frozen' WHERE work_id=?", (work_id,))
                append_event(connection, actor_id=actor_id, action="work.admitted",
                             resource_type="work", resource_id=work_id,
                             detail={"work_id": work_id, "version_id": work["frozen_version_id"],
                                     "status": "frozen"},
                             occurred_at=self._now())
                return "work", work_id, {"work_id": work_id, "status": "frozen"}

            return self._idempotent(connection, request_id=request_id,
                                    action="admit_quarantine", payload=payload, create=create)

    # ------------------------------------------------------------ 回避分派

    def declare_conflict(self, *, request_id: str, actor_id: str, reviewer_actor_id: str,
                         conflict_key: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "reviewer_actor_id": reviewer_actor_id,
                   "conflict_key": conflict_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reviewer = self._actor(connection, reviewer_actor_id)
            if reviewer.role != "reviewer":
                raise ValidationError("冲突声明只能登记在评委身上")
            conflict_key = self._identifier(conflict_key, "conflict_key")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT 1 FROM reviewer_conflicts WHERE reviewer_actor_id=? AND conflict_key=?",
                    (reviewer_actor_id, conflict_key),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO reviewer_conflicts(reviewer_actor_id,conflict_key,created_at) "
                        "VALUES(?,?,?)",
                        (reviewer_actor_id, conflict_key, self._now()),
                    )
                    append_event(connection, actor_id=actor_id, action="conflict.declared",
                                 resource_type="reviewer_conflict",
                                 resource_id=f"{reviewer_actor_id}:{conflict_key}",
                                 detail={"reviewer_actor_id": reviewer_actor_id,
                                         "conflict_key": conflict_key},
                                 occurred_at=self._now())
                return "reviewer_conflict", f"{reviewer_actor_id}:{conflict_key}", {
                    "reviewer_actor_id": reviewer_actor_id, "conflict_key": conflict_key}

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_conflict", payload=payload, create=create)

    def assign_reviewers(self, *, request_id: str, actor_id: str, competition_id: str,
                         review_count: int = DEFAULT_REVIEW_COUNT) -> WriteReceipt:
        if not isinstance(review_count, int) or review_count < 1:
            raise ValidationError("review_count 必须是不小于 1 的整数")
        payload = {"actor_id": actor_id, "competition_id": competition_id,
                   "review_count": review_count}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            competition = self._competition(connection, competition_id)
            if competition["status"] != "frozen":
                raise ConflictError("赛事冻结后才能分派评委")
            works = connection.execute(
                "SELECT * FROM works WHERE competition_id=? AND status='frozen' ORDER BY work_id",
                (competition_id,),
            ).fetchall()
            created: list[str] = []

            def create() -> tuple[str, str, dict[str, Any]]:
                planned = connection.execute(
                    "SELECT review_count FROM competitions WHERE competition_id=?",
                    (competition_id,),
                ).fetchone()["review_count"]
                if planned is None:
                    connection.execute(
                        "UPDATE competitions SET review_count=? WHERE competition_id=?",
                        (review_count, competition_id),
                    )
                elif planned != review_count:
                    raise ConflictError(f"该赛事已按每作品 {planned} 名评委冻结分派，不能更改")
                for work in works:
                    existing = connection.execute(
                        "SELECT COUNT(*) AS count FROM assignments WHERE work_id=? AND status='assigned'",
                        (work["work_id"],),
                    ).fetchone()["count"]
                    needed = review_count - existing
                    if needed <= 0:
                        continue
                    eligible = self._eligible_reviewers(connection, work)
                    if len(eligible) < needed:
                        raise ConflictError(
                            f"作品 {work['work_id']} 可分派评委不足，需要 {needed} 名符合回避规则的评委")
                    for reviewer in eligible[:needed]:
                        assignment_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO assignments(assignment_id,competition_id,work_id,"
                            "reviewer_actor_id,status,created_at) VALUES(?,?,?,?,'assigned',?)",
                            (assignment_id, competition_id, work["work_id"],
                             reviewer.actor_id, self._now()),
                        )
                        created.append(assignment_id)
                        append_event(connection, actor_id=actor_id, action="reviewer.assigned",
                                     resource_type="assignment", resource_id=assignment_id,
                                     detail={"work_id": work["work_id"],
                                             "reviewer_actor_id": reviewer.actor_id,
                                             "version_id": work["frozen_version_id"]},
                                     occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="assignment.completed",
                             resource_type="competition", resource_id=competition_id,
                             detail={"competition_id": competition_id, "assignments_created": len(created)},
                             occurred_at=self._now())
                return "competition", competition_id, {
                    "competition_id": competition_id, "assignments_created": len(created)}

            return self._idempotent(connection, request_id=request_id,
                                    action="assign_reviewers", payload=payload, create=create)

    def recuse(self, *, request_id: str, actor_id: str, assignment_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "assignment_id": assignment_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("分派不存在")
            if actor.role != "admin" and actor.actor_id != row["reviewer_actor_id"]:
                raise PermissionDenied("只能对自己的分派提出回避")
            self._require(actor, "admin", "reviewer")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] != "assigned":
                    raise ConflictError("该分派已经处于回避状态")
                already = connection.execute(
                    "SELECT COUNT(*) AS count FROM scores WHERE assignment_id=?", (assignment_id,)
                ).fetchone()["count"]
                if already:
                    raise ConflictError("已经做出评分决定的分派不能回避")
                connection.execute(
                    "UPDATE assignments SET status='recused', reason=? WHERE assignment_id=?",
                    (reason, assignment_id),
                )
                append_event(connection, actor_id=actor_id, action="reviewer.recused",
                             resource_type="assignment", resource_id=assignment_id,
                             detail={"work_id": row["work_id"],
                                     "reviewer_actor_id": row["reviewer_actor_id"], "reason": reason},
                             occurred_at=self._now())
                # 回避后立即按同一回避规则补位。
                work = self._work(connection, row["work_id"])
                target = connection.execute(
                    "SELECT review_count FROM competitions WHERE competition_id=?",
                    (work["competition_id"],),
                ).fetchone()["review_count"] or DEFAULT_REVIEW_COUNT
                active = connection.execute(
                    "SELECT COUNT(*) AS count FROM assignments WHERE work_id=? AND status='assigned'",
                    (row["work_id"],),
                ).fetchone()["count"]
                backfills: list[str] = []
                while active < target:
                    eligible = self._eligible_reviewers(connection, work)
                    if not eligible:
                        break
                    reviewer = eligible[0]
                    new_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO assignments(assignment_id,competition_id,work_id,"
                        "reviewer_actor_id,status,created_at) VALUES(?,?,?,?,'assigned',?)",
                        (new_id, work["competition_id"], work["work_id"],
                         reviewer.actor_id, self._now()),
                    )
                    backfills.append(new_id)
                    append_event(connection, actor_id=actor_id, action="reviewer.assigned",
                                 resource_type="assignment", resource_id=new_id,
                                 detail={"work_id": work["work_id"],
                                         "reviewer_actor_id": reviewer.actor_id,
                                         "version_id": work["frozen_version_id"],
                                         "backfill_for": assignment_id},
                                 occurred_at=self._now())
                    active += 1
                return "assignment", assignment_id, {"assignment_id": assignment_id,
                                                      "backfills": backfills}

            return self._idempotent(connection, request_id=request_id,
                                    action="recuse", payload=payload, create=create)

    # ---------------------------------------------------------------- 评分

    def submit_score(self, *, request_id: str, actor_id: str, assignment_id: str,
                     version_id: str, dimension: str, points: int, comment: str
                     ) -> tuple[ScoreRecord, str]:
        """提交评分，返回 (决定, 结果类别)。

        结果类别：created 新决定；replayed 同一请求重放；duplicate 重复评分返回原决定。
        旧决定不可改写：换请求提交不同内容会暴露冲突；同一请求提交不同内容也会暴露冲突。
        """

        if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= 100:
            raise ValidationError("points 必须是 0 到 100 的整数")
        if dimension not in SCORE_DIMENSIONS:
            raise ValidationError(f"dimension 必须是 {sorted(SCORE_DIMENSIONS)} 之一")
        payload = {"actor_id": actor_id, "assignment_id": assignment_id, "version_id": version_id,
                   "dimension": dimension, "points": points, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            assignment = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None:
                raise NotFoundError("分派不存在")
            if assignment["reviewer_actor_id"] != actor_id:
                raise PermissionDenied("不能为他人的分派评分")
            if assignment["status"] != "assigned":
                raise ConflictError("该分派已回避，不能评分")
            work = self._work(connection, assignment["work_id"])
            if work["status"] != "frozen":
                raise ConflictError("作品未处于可评审状态（可能待补证）")
            # 评分必须引用冻结送审的具体版本，防止分数对错评审版本。
            if version_id != work["frozen_version_id"]:
                raise ConflictError("评分必须引用当前冻结送审的具体版本")
            comment = self._text(comment, "comment", 2000)

            existing = connection.execute(
                "SELECT * FROM scores WHERE assignment_id=? AND dimension=?",
                (assignment_id, dimension),
            ).fetchone()
            if existing is not None:
                if existing["request_id"] == request_id and \
                        existing["points"] == points and existing["comment"] == comment:
                    return self._to_score(existing), "replayed"
                if existing["points"] == points and existing["comment"] == comment:
                    # 内容完全相同的重复评分：返回原决定，不产生新记录。
                    return self._to_score(existing), "duplicate"
                raise ConflictError(
                    "该维度已有不可改写的评分决定；不同分值不能复用，如需变更请走申诉复核")

            def create() -> tuple[str, str, dict[str, Any]]:
                score_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO scores(score_id,assignment_id,request_id,version_id,dimension,"
                    "points,comment,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (score_id, assignment_id, request_id, version_id, dimension, points,
                     comment, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="score.submitted",
                             resource_type="score", resource_id=score_id,
                             detail={"work_id": work["work_id"], "assignment_id": assignment_id,
                                     "version_id": version_id, "dimension": dimension,
                                     "points": points},
                             occurred_at=self._now())
                return "score", score_id, {"score_id": score_id}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="submit_score", payload=payload, create=create)
            row = connection.execute("SELECT * FROM scores WHERE score_id=?",
                                     (receipt.resource_id,)).fetchone()
            return self._to_score(row), "replayed" if receipt.replayed else "created"

    def _to_score(self, row) -> ScoreRecord:
        return ScoreRecord(row["score_id"], row["assignment_id"], row["request_id"],
                           row["version_id"], row["dimension"], row["points"], row["comment"],
                           row["created_by"], row["created_at"])

    # ---------------------------------------------------------------- 申诉

    def open_appeal(self, *, request_id: str, actor_id: str, work_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            work = self._work(connection, work_id)
            reason = self._text(reason, "reason", 2000)

            def create() -> tuple[str, str, dict[str, Any]]:
                if work["status"] != "frozen":
                    raise ConflictError("只有已送审作品可以提出申诉")
                scored = connection.execute(
                    "SELECT COUNT(*) AS count FROM scores s JOIN assignments a ON s.assignment_id=a.assignment_id "
                    "WHERE a.work_id=?", (work_id,)
                ).fetchone()["count"]
                if not scored:
                    raise ConflictError("作品尚无评分决定，不能申诉")
                existing = connection.execute(
                    "SELECT appeal_id FROM appeals WHERE work_id=?", (work_id,)
                ).fetchone()
                if existing:
                    raise ConflictError("该作品已经存在申诉")
                appeal_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO appeals(appeal_id,work_id,reason,status,created_by,created_at) "
                    "VALUES(?,?,?,'open',?,?)",
                    (appeal_id, work_id, reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="appeal.opened",
                             resource_type="appeal", resource_id=appeal_id,
                             detail={"work_id": work_id, "reason": reason},
                             occurred_at=self._now())
                return "appeal", appeal_id, {"appeal_id": appeal_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="open_appeal", payload=payload, create=create)

    def submit_appeal_review(self, *, request_id: str, actor_id: str, appeal_id: str,
                             assignment_id: str, dimension: str, review_points: int,
                             rationale: str) -> WriteReceipt:
        if not isinstance(review_points, int) or isinstance(review_points, bool) \
                or not 0 <= review_points <= 100:
            raise ValidationError("review_points 必须是 0 到 100 的整数")
        if dimension not in SCORE_DIMENSIONS:
            raise ValidationError(f"dimension 必须是 {sorted(SCORE_DIMENSIONS)} 之一")
        payload = {"actor_id": actor_id, "appeal_id": appeal_id, "assignment_id": assignment_id,
                   "dimension": dimension, "review_points": review_points, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            appeal = connection.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
            if appeal is None:
                raise NotFoundError("申诉不存在")
            assignment = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None or assignment["work_id"] != appeal["work_id"]:
                raise ValidationError("分派不存在或不属于申诉作品")
            if actor.role != "admin":
                # 复核人必须是同一评审组的成员，且不能复核自己做出的原决定（独立复核）。
                if actor.actor_id == assignment["reviewer_actor_id"]:
                    raise PermissionDenied("不能复核自己做出的评分决定，须由评审组其他成员复核")
                panel = connection.execute(
                    "SELECT 1 FROM assignments WHERE work_id=? AND reviewer_actor_id=? "
                    "AND status='assigned' LIMIT 1",
                    (appeal["work_id"], actor.actor_id),
                ).fetchone()
                if panel is None:
                    raise PermissionDenied("复核人必须是该作品评审组的成员")
            original = connection.execute(
                "SELECT * FROM scores WHERE assignment_id=? AND dimension=?",
                (assignment_id, dimension),
            ).fetchone()
            if original is None:
                raise NotFoundError("该维度没有原评分决定")
            rationale = self._text(rationale, "rationale", 2000)

            def create() -> tuple[str, str, dict[str, Any]]:
                if appeal["status"] != "open":
                    raise ConflictError("申诉已经裁决，不能再提交复核")
                duplicate = connection.execute(
                    "SELECT 1 FROM appeal_reviews WHERE appeal_id=? AND assignment_id=? AND dimension=?",
                    (appeal_id, assignment_id, dimension),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("该维度已有不可改写的复核决定")
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO appeal_reviews(review_id,appeal_id,assignment_id,dimension,"
                    "original_score_id,review_points,rationale,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (review_id, appeal_id, assignment_id, dimension, original["score_id"],
                     review_points, rationale, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="appeal.reviewed",
                             resource_type="appeal_review", resource_id=review_id,
                             detail={"work_id": appeal["work_id"], "appeal_id": appeal_id,
                                     "assignment_id": assignment_id, "dimension": dimension,
                                     "original_points": original["points"],
                                     "review_points": review_points,
                                     "final_points": None, "outcome": None},
                             occurred_at=self._now())
                return "appeal_review", review_id, {"review_id": review_id, "final_points": None}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_appeal_review", payload=payload, create=create)

    def decide_appeal(self, *, request_id: str, actor_id: str, appeal_id: str) -> WriteReceipt:
        """按规则对每个已复核维度生成最终结果：差距达到阈值则采用复核分，否则维持原分。"""

        payload = {"actor_id": actor_id, "appeal_id": appeal_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            appeal = connection.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
            if appeal is None:
                raise NotFoundError("申诉不存在")
            work_id = appeal["work_id"]
            decided_at = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                if appeal["status"] != "open":
                    raise ConflictError("申诉已经裁决")
                reviews = connection.execute(
                    "SELECT * FROM appeal_reviews WHERE appeal_id=? ORDER BY created_at, review_id",
                    (appeal_id,),
                ).fetchall()
                if not reviews:
                    raise ConflictError("申诉尚无复核意见，不能裁决")
                decisions: list[dict[str, Any]] = []
                for review in reviews:
                    original = connection.execute(
                        "SELECT points FROM scores WHERE score_id=?",
                        (review["original_score_id"],),
                    ).fetchone()
                    original_points = original["points"]
                    if abs(review["review_points"] - original_points) >= APPEAL_CHANGE_THRESHOLD:
                        final_points = review["review_points"]
                        outcome = "changed"
                    else:
                        final_points = original_points
                        outcome = "kept"
                    connection.execute(
                        "UPDATE appeal_reviews SET final_points=?, outcome=?, decided_at=? WHERE review_id=?",
                        (final_points, outcome, decided_at, review["review_id"]),
                    )
                    decisions.append({"dimension": review["dimension"],
                                      "assignment_id": review["assignment_id"],
                                      "original_points": original_points,
                                      "review_points": review["review_points"],
                                      "final_points": final_points, "outcome": outcome})
                    append_event(connection, actor_id=actor_id, action="appeal.decided_dimension",
                                 resource_type="appeal_review", resource_id=review["review_id"],
                                 detail={"work_id": work_id, "appeal_id": appeal_id,
                                         "assignment_id": review["assignment_id"],
                                         "dimension": review["dimension"],
                                         "original_points": original_points,
                                         "review_points": review["review_points"],
                                         "final_points": final_points, "outcome": outcome,
                                         "threshold": APPEAL_CHANGE_THRESHOLD},
                                 occurred_at=decided_at)
                connection.execute(
                    "UPDATE appeals SET status='decided', decided_at=? WHERE appeal_id=?",
                    (decided_at, appeal_id),
                )
                append_event(connection, actor_id=actor_id, action="appeal.decided",
                             resource_type="appeal", resource_id=appeal_id,
                             detail={"work_id": work_id, "decisions": decisions,
                                     "threshold": APPEAL_CHANGE_THRESHOLD},
                             occurred_at=decided_at)
                return "appeal", appeal_id, {"appeal_id": appeal_id, "status": "decided",
                                             "decisions": len(decisions)}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_appeal", payload=payload, create=create)

    # ---------------------------------------------------------------- 查询

    def get_work(self, work_id: str) -> WorkRecord:
        row = self.database.connection.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return WorkRecord(row["work_id"], row["competition_id"], row["title"], row["author_name"],
                          row["author_actor_id"], row["author_org_id"], row["status"],
                          tuple(json.loads(row["required_credentials_json"])),
                          row["frozen_version_id"])

    def list_versions(self, work_id: str) -> list[WorkVersion]:
        self._work(self.database.connection, work_id)
        rows = self.database.connection.execute(
            "SELECT * FROM work_versions WHERE work_id=? ORDER BY sequence_no", (work_id,)
        ).fetchall()
        return [self._to_version(row) for row in rows]

    def list_assignments(self, competition_id: str | None = None, work_id: str | None = None) -> list[Assignment]:
        sql = "SELECT * FROM assignments"
        clauses: list[str] = []
        parameters: list[Any] = []
        if competition_id:
            clauses.append("competition_id=?")
            parameters.append(competition_id)
        if work_id:
            clauses.append("work_id=?")
            parameters.append(work_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY work_id, created_at, assignment_id"
        return [Assignment(r["assignment_id"], r["competition_id"], r["work_id"],
                           r["reviewer_actor_id"], r["status"], r["reason"], r["created_at"])
                for r in self.database.connection.execute(sql, parameters)]

    def list_decisions(self, work_id: str) -> list[dict[str, Any]]:
        """返回作品每个评分维度的当前有效决定（含申诉后的最终结果）。"""

        self._work(self.database.connection, work_id)
        rows = self.database.connection.execute(
            "SELECT s.*, a.assignment_id AS a_id, a.reviewer_actor_id FROM scores s "
            "JOIN assignments a ON s.assignment_id=a.assignment_id WHERE a.work_id=? "
            "ORDER BY s.dimension, s.created_at",
            (work_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            final = self.database.connection.execute(
                "SELECT final_points, outcome FROM appeal_reviews WHERE original_score_id=? "
                "AND outcome IS NOT NULL LIMIT 1",
                (row["score_id"],),
            ).fetchone()
            result.append({
                "score_id": row["score_id"], "assignment_id": row["assignment_id"],
                "reviewer_actor_id": row["reviewer_actor_id"], "version_id": row["version_id"],
                "dimension": row["dimension"], "points": row["points"],
                "final_points": final["final_points"] if final else row["points"],
                "appeal_outcome": final["outcome"] if final else None,
            })
        return result

    def _mask_name(self, name: str) -> str:
        if len(name) <= 1:
            return "＊"
        return name[0] + "＊" * (len(name) - 1)

    def public_status(self, work_id: str) -> dict[str, Any]:
        """公开查询：仅展示脱敏汇总，不出现作者身份、凭据或评语明细。"""

        work = self.get_work(work_id)
        decisions = self.list_decisions(work_id)
        dimensions: dict[str, list[int]] = {}
        for decision in decisions:
            dimensions.setdefault(decision["dimension"], []).append(decision["final_points"])
        summary = []
        for dimension in sorted(dimensions):
            values = dimensions[dimension]
            summary.append({"dimension": dimension, "decision_count": len(values),
                            "average_points": round(sum(values) / len(values), 2)})
        all_points = [point for values in dimensions.values() for point in values]
        version_seq = None
        package_prefix = None
        if work.frozen_version_id:
            version = self._version(self.database.connection, work.frozen_version_id)
            version_seq = version["sequence_no"]
            package_prefix = version["package_sha256"][:12]
        status_text = {
            "pending_evidence": "pending_evidence",
            "submittable": "submittable",
            "frozen": "under_review",
            "quarantined": "evidence_required",
        }[work.status]
        return {
            "work_id": work.work_id,
            "title_masked": self._mask_name(work.title),
            "author_masked": self._mask_name(work.author_name),
            "status": status_text,
            "frozen_version": None if version_seq is None else {
                "sequence_no": version_seq, "package_sha256_prefix": package_prefix},
            "dimensions": summary,
            "overall_average": round(sum(all_points) / len(all_points), 2) if all_points else None,
        }

    def work_audit_detail(self, *, actor_id: str, work_id: str) -> dict[str, Any]:
        """审计查询：可追到素材凭据、版本快照、分派回避与每次评分/申诉决定。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            work = self._work(connection, work_id)
            versions = [self._to_version(r).__dict__ for r in connection.execute(
                "SELECT * FROM work_versions WHERE work_id=? ORDER BY sequence_no", (work_id,))]
            credentials = [dict(r) for r in connection.execute(
                "SELECT credential_id,version_id,item_id,license_code,evidence_ref,evidence_hash,"
                "recorded_by,created_at FROM credential_records WHERE work_id=? ORDER BY created_at",
                (work_id,))]
            snapshot = connection.execute(
                "SELECT * FROM frozen_snapshots WHERE work_id=?", (work_id,)
            ).fetchone()
            snapshot_dict = None
            if snapshot:
                snapshot_dict = {key: snapshot[key] for key in snapshot.keys()}
                for key in ("metadata_json", "manifest_json", "credentials_json"):
                    snapshot_dict[key] = json.loads(snapshot_dict[key])
            assignments = [dict(r) for r in connection.execute(
                "SELECT * FROM assignments WHERE work_id=? ORDER BY created_at", (work_id,))]
            scores = []
            for score_row in connection.execute(
                "SELECT * FROM scores s JOIN assignments a ON s.assignment_id=a.assignment_id "
                "WHERE a.work_id=? ORDER BY s.created_at", (work_id,)
            ):
                item = {k: score_row[k] for k in
                        ("score_id", "assignment_id", "request_id", "version_id", "dimension",
                         "points", "comment", "created_by", "created_at")}
                review = connection.execute(
                    "SELECT * FROM appeal_reviews WHERE original_score_id=? ORDER BY created_at LIMIT 1",
                    (score_row["score_id"],),
                ).fetchone()
                item["appeal_review"] = None if review is None else {
                    "review_id": review["review_id"], "appeal_id": review["appeal_id"],
                    "review_points": review["review_points"], "rationale": review["rationale"],
                    "final_points": review["final_points"], "outcome": review["outcome"],
                    "decided_at": review["decided_at"]}
                scores.append(item)
            appeal = connection.execute(
                "SELECT * FROM appeals WHERE work_id=?", (work_id,)
            ).fetchone()
            appeal_dict = None
            if appeal:
                appeal_dict = {k: appeal[k] for k in appeal.keys()}
                appeal_dict["reviews"] = [dict(r) for r in connection.execute(
                    "SELECT * FROM appeal_reviews WHERE appeal_id=? ORDER BY created_at",
                    (appeal["appeal_id"],))]
            events = [dict(r) for r in connection.execute(
                "SELECT sequence,event_id,actor_id,action,resource_type,resource_id,detail_json,"
                "previous_hash,event_hash,occurred_at FROM audit_events "
                "WHERE json_extract(detail_json,'$.work_id')=? OR resource_id=? "
                "ORDER BY sequence", (work_id, work_id))]
            for event in events:
                event["detail"] = json.loads(event.pop("detail_json"))
            return {
                "work": {
                    "work_id": work["work_id"], "competition_id": work["competition_id"],
                    "title": work["title"], "author_name": work["author_name"],
                    "author_actor_id": work["author_actor_id"], "author_org_id": work["author_org_id"],
                    "status": work["status"],
                    "required_credentials": self._required(work),
                    "frozen_version_id": work["frozen_version_id"],
                },
                "versions": versions,
                "credentials": credentials,
                "frozen_snapshot": snapshot_dict,
                "assignments": assignments,
                "scores": scores,
                "appeal": appeal_dict,
                "audit_events": events,
            }
