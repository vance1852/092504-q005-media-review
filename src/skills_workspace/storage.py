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
    title TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'frozen')),
    review_count INTEGER,
    frozen_at TEXT
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
    title TEXT NOT NULL,
    author_name TEXT NOT NULL,
    author_actor_id TEXT,
    author_org_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending_evidence', 'submittable', 'frozen', 'quarantined')),
    required_credentials_json TEXT NOT NULL,
    frozen_version_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_versions (
    version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    sequence_no INTEGER NOT NULL CHECK(sequence_no >= 1),
    package_name TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    script_summary TEXT NOT NULL,
    interaction_notes TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    credentials_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS credential_records (
    credential_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(work_id),
    version_id TEXT REFERENCES work_versions(version_id),
    item_id TEXT NOT NULL,
    license_code TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_hash TEXT,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, item_id)
);
CREATE TABLE IF NOT EXISTS frozen_snapshots (
    work_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    package_name TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    script_summary TEXT NOT NULL,
    interaction_notes TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    credentials_json TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    PRIMARY KEY (work_id, version_id)
);
CREATE TABLE IF NOT EXISTS reviewer_conflicts (
    reviewer_actor_id TEXT NOT NULL,
    conflict_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (reviewer_actor_id, conflict_key)
);
CREATE TABLE IF NOT EXISTS assignments (
    assignment_id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
    work_id TEXT NOT NULL REFERENCES works(work_id),
    reviewer_actor_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('assigned', 'recused')),
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, reviewer_actor_id)
);
CREATE TABLE IF NOT EXISTS scores (
    score_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id),
    request_id TEXT NOT NULL UNIQUE,
    version_id TEXT NOT NULL REFERENCES work_versions(version_id),
    dimension TEXT NOT NULL,
    points INTEGER NOT NULL CHECK(points >= 0 AND points <= 100),
    comment TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(assignment_id, dimension)
);
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL UNIQUE REFERENCES works(work_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'decided')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS appeal_reviews (
    review_id TEXT PRIMARY KEY,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id),
    dimension TEXT NOT NULL,
    original_score_id TEXT NOT NULL REFERENCES scores(score_id),
    review_points INTEGER NOT NULL CHECK(review_points >= 0 AND review_points <= 100),
    rationale TEXT NOT NULL,
    final_points INTEGER,
    outcome TEXT CHECK(outcome IN ('kept', 'changed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(appeal_id, assignment_id, dimension)
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
