"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示训练或赛事组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class WorkVersion:
    """一次不可改写的作品提交版本（仅登记摘要与元数据，不含媒体内容）。"""

    version_id: str
    work_id: str
    sequence_no: int
    package_name: str
    package_sha256: str
    script_summary: str
    interaction_notes: str
    metadata: dict[str, Any]
    manifest: list[dict[str, Any]]
    manifest_hash: str
    credentials: dict[str, str]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WorkRecord:
    """作品及其当前送审状态。"""

    work_id: str
    competition_id: str
    title: str
    author_name: str
    author_actor_id: str | None
    author_org_id: str | None
    status: str
    required_credentials: tuple[str, ...]
    frozen_version_id: str | None


@dataclass(frozen=True)
class Assignment:
    """一件作品与一名评委之间的分派（含回避状态）。"""

    assignment_id: str
    competition_id: str
    work_id: str
    reviewer_actor_id: str
    status: str
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class ScoreRecord:
    """绑定到具体送审版本与评分维度的决定。"""

    score_id: str
    assignment_id: str
    request_id: str
    version_id: str
    dimension: str
    points: int
    comment: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class AppealDecision:
    """申诉复核维度：原分、复核分与按规则生成的最终结果。"""

    review_id: str
    appeal_id: str
    assignment_id: str
    dimension: str
    original_points: int
    review_points: int
    final_points: int
    outcome: str
    decided_at: str | None
