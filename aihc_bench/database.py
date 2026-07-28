from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .schema import utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS commits (
  sha TEXT PRIMARY KEY,
  ordinal INTEGER NOT NULL,
  committed_at TEXT NOT NULL,
  subject TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS commits_ordinal ON commits(ordinal);

CREATE TABLE IF NOT EXISTS attempts (
  experiment_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  commit_sha TEXT NOT NULL REFERENCES commits(sha),
  run_id TEXT NOT NULL,
  status TEXT NOT NULL,
  unavailable_reason TEXT,
  detail TEXT,
  environment_json TEXT NOT NULL,
  result_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY (experiment_id, platform, commit_sha)
);

CREATE INDEX IF NOT EXISTS attempts_lookup
  ON attempts(experiment_id, platform, status);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def replace_commits(self, commits: Iterable[Dict[str, Any]]) -> None:
        rows = [(item["sha"], item["ordinal"], item["committed_at"], item["subject"]) for item in commits]
        with self.connection:
            self.connection.executemany(
                "INSERT INTO commits(sha, ordinal, committed_at, subject) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(sha) DO UPDATE SET ordinal=excluded.ordinal, committed_at=excluded.committed_at, subject=excluded.subject",
                rows,
            )

    def commits(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT sha, ordinal, committed_at, subject FROM commits ORDER BY ordinal").fetchall()
        return [dict(row) for row in rows]

    def terminal_attempts(self, experiment_id: str, platform_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT a.*, c.ordinal FROM attempts a JOIN commits c ON c.sha=a.commit_sha "
            "WHERE experiment_id=? AND platform=? AND status != 'running' ORDER BY c.ordinal",
            (experiment_id, platform_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def start_attempt(
        self,
        experiment_id: str,
        platform_id: str,
        commit_sha: str,
        run_id: str,
        environment: Dict[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO attempts(experiment_id, platform, commit_sha, run_id, status, environment_json, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?) "
                "ON CONFLICT(experiment_id, platform, commit_sha) DO UPDATE SET "
                "run_id=excluded.run_id, status='running', unavailable_reason=NULL, detail=NULL, "
                "environment_json=excluded.environment_json, result_json=NULL, started_at=excluded.started_at, finished_at=NULL",
                (experiment_id, platform_id, commit_sha, run_id, json.dumps(environment, sort_keys=True), utc_now()),
            )

    def finish_attempt(
        self,
        experiment_id: str,
        platform_id: str,
        commit_sha: str,
        status: str,
        result: Dict[str, Any],
        unavailable_reason: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE attempts SET status=?, unavailable_reason=?, detail=?, result_json=?, finished_at=? "
                "WHERE experiment_id=? AND platform=? AND commit_sha=?",
                (
                    status,
                    unavailable_reason,
                    detail,
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                    experiment_id,
                    platform_id,
                    commit_sha,
                ),
            )

    def forget(self, experiment_id: str, platform_id: str, commit_sha: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM attempts WHERE experiment_id=? AND platform=? AND commit_sha=?",
                (experiment_id, platform_id, commit_sha),
            )
        return cursor.rowcount > 0

    def result_envelopes(self, experiment_id: Optional[str] = None, platform_id: Optional[str] = None) -> List[Dict[str, Any]]:
        conditions = ["status != 'running'", "result_json IS NOT NULL"]
        parameters: List[str] = []
        if experiment_id:
            conditions.append("experiment_id=?")
            parameters.append(experiment_id)
        if platform_id:
            conditions.append("platform=?")
            parameters.append(platform_id)
        rows = self.connection.execute(
            "SELECT result_json FROM attempts WHERE " + " AND ".join(conditions) + " ORDER BY finished_at",
            parameters,
        ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def latest_attempt(self, experiment_id: str, platform_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            "SELECT a.*, c.ordinal, c.committed_at, c.subject FROM attempts a JOIN commits c ON c.sha=a.commit_sha "
            "WHERE experiment_id=? AND platform=? AND a.status != 'running' ORDER BY c.ordinal DESC LIMIT 1",
            (experiment_id, platform_id),
        ).fetchone()
        return dict(row) if row else None
