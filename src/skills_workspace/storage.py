"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitions (
    competition_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    title TEXT NOT NULL,
    deadline TEXT NOT NULL,
    reviewers_per_work INTEGER NOT NULL CHECK(reviewers_per_work BETWEEN 1 AND 9),
    score_tolerance REAL NOT NULL CHECK(score_tolerance >= 0),
    rubric_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','frozen')) DEFAULT 'open',
    frozen_at TEXT
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
    author_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    author_organization_id TEXT NOT NULL,
    title TEXT NOT NULL,
    pseudonym TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(competition_id, author_actor_id)
);
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    package_name TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    script_summary_json TEXT NOT NULL,
    material_manifest_json TEXT NOT NULL,
    interaction_notes_json TEXT NOT NULL,
    author_declaration_json TEXT NOT NULL,
    delivery_checklist_json TEXT NOT NULL,
    evidence_status TEXT NOT NULL CHECK(evidence_status IN ('complete','pending_evidence')),
    evidence_gap_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(work_id, version_no),
    UNIQUE(work_id, content_digest)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL UNIQUE REFERENCES competitions(competition_id),
    deadline TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_versions (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(submission_id),
    version_no INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    evidence_status TEXT NOT NULL CHECK(evidence_status IN ('complete','pending_evidence')),
    PRIMARY KEY(snapshot_id, work_id)
);
CREATE TABLE IF NOT EXISTS reviewer_conflicts (
    conflict_id TEXT PRIMARY KEY,
    reviewer_id TEXT NOT NULL REFERENCES actors(actor_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(reviewer_id, work_id)
);
CREATE TABLE IF NOT EXISTS review_assignments (
    assignment_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    reviewer_id TEXT NOT NULL REFERENCES actors(actor_id),
    slot INTEGER NOT NULL CHECK(slot >= 1),
    status TEXT NOT NULL CHECK(status IN ('assigned','recused')),
    assigned_at TEXT NOT NULL,
    UNIQUE(snapshot_id, work_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS score_decisions (
    decision_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL UNIQUE REFERENCES review_assignments(assignment_id),
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    dimensions_json TEXT NOT NULL,
    total REAL NOT NULL,
    rationale TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','ruled')) DEFAULT 'open',
    filed_by TEXT NOT NULL REFERENCES actors(actor_id),
    final_total REAL,
    final_basis TEXT,
    created_at TEXT NOT NULL,
    ruled_at TEXT
);
CREATE TABLE IF NOT EXISTS appeal_reviews (
    review_id TEXT PRIMARY KEY,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    reviewer_id TEXT NOT NULL REFERENCES actors(actor_id),
    dimensions_json TEXT NOT NULL,
    total REAL NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(appeal_id, reviewer_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
