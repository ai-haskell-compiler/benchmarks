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
  subject TEXT NOT NULL,
  tree_key TEXT
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
  machine_id TEXT,
  inherited_from TEXT,
  uploaded_at TEXT,
  result_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY (experiment_id, platform, commit_sha)
);

CREATE INDEX IF NOT EXISTS attempts_lookup
  ON attempts(experiment_id, platform, status);

-- Ad-hoc comparisons stay local: the uploader never reads this table.
CREATE TABLE IF NOT EXISTS adhoc_runs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  a_label TEXT NOT NULL,
  b_label TEXT NOT NULL,
  report_json TEXT NOT NULL
);
"""

MIGRATIONS = {
    "commits": {"tree_key": "TEXT"},
    "attempts": {"machine_id": "TEXT", "inherited_from": "TEXT", "uploaded_at": "TEXT"},
}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in existing:
                    with self.connection:
                        self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self.connection.close()

    def replace_commits(self, commits: Iterable[Dict[str, Any]]) -> None:
        """Make the commits table match ``commits`` exactly.

        Commits no longer in the history, such as those that fell before the
        ``aihc_since`` cutoff, are removed together with their attempts.
        """
        rows = [
            (item["sha"], item["ordinal"], item["committed_at"], item["subject"], item.get("tree_key"))
            for item in commits
        ]
        with self.connection:
            self.connection.execute("CREATE TEMP TABLE IF NOT EXISTS kept_commits(sha TEXT PRIMARY KEY)")
            self.connection.execute("DELETE FROM kept_commits")
            self.connection.executemany("INSERT INTO kept_commits(sha) VALUES (?)", [(row[0],) for row in rows])
            self.connection.execute("DELETE FROM attempts WHERE commit_sha NOT IN (SELECT sha FROM kept_commits)")
            self.connection.execute("DELETE FROM commits WHERE sha NOT IN (SELECT sha FROM kept_commits)")
            self.connection.executemany(
                "INSERT INTO commits(sha, ordinal, committed_at, subject, tree_key) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(sha) DO UPDATE SET ordinal=excluded.ordinal, committed_at=excluded.committed_at, "
                "subject=excluded.subject, tree_key=COALESCE(excluded.tree_key, commits.tree_key)",
                rows,
            )

    def commits(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT sha, ordinal, committed_at, subject, tree_key FROM commits ORDER BY ordinal"
        ).fetchall()
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
        machine_id: Optional[str] = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO attempts(experiment_id, platform, commit_sha, run_id, status, environment_json, machine_id, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?, ?) "
                "ON CONFLICT(experiment_id, platform, commit_sha) DO UPDATE SET "
                "run_id=excluded.run_id, status='running', unavailable_reason=NULL, detail=NULL, "
                "environment_json=excluded.environment_json, machine_id=excluded.machine_id, inherited_from=NULL, "
                "uploaded_at=NULL, result_json=NULL, started_at=excluded.started_at, finished_at=NULL",
                (experiment_id, platform_id, commit_sha, run_id, json.dumps(environment, sort_keys=True), machine_id, utc_now()),
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

    def propagate_inherited(self, experiment_id: str, platform_id: str) -> int:
        """Copy measured results to commits that build the same compiler.

        Every commit whose ``tree_key`` matches a measured, non-inherited
        commit and that has no measurement of its own receives an
        ``inherited`` attempt carrying a copy of the source envelope. The
        nearest source by ordinal wins. Returns the number of attempts written.
        """
        commits = self.commits()
        by_key: Dict[str, List[Dict[str, Any]]] = {}
        attempts = {attempt["commit_sha"]: attempt for attempt in self.terminal_attempts(experiment_id, platform_id)}
        for commit in commits:
            attempt = attempts.get(commit["sha"])
            if commit.get("tree_key") and attempt and not attempt.get("inherited_from") and attempt.get("result_json"):
                by_key.setdefault(commit["tree_key"], []).append({**commit, "attempt": attempt})

        written = 0
        with self.connection:
            for commit in commits:
                key = commit.get("tree_key")
                if not key or key not in by_key:
                    continue
                current = attempts.get(commit["sha"])
                if current and not current.get("inherited_from"):
                    continue
                source = min(by_key[key], key=lambda item: (abs(item["ordinal"] - commit["ordinal"]), -item["ordinal"]))
                if current and current.get("inherited_from") == source["sha"]:
                    continue
                attempt = source["attempt"]
                envelope = json.loads(attempt["result_json"])
                envelope["aihc_commit"] = {k: v for k, v in commit.items() if k != "tree_key"}
                envelope["inherited_from"] = source["sha"]
                self.connection.execute(
                    "INSERT INTO attempts(experiment_id, platform, commit_sha, run_id, status, unavailable_reason, detail, "
                    "environment_json, machine_id, inherited_from, result_json, started_at, finished_at) "
                    "VALUES (?, ?, ?, ?, 'inherited', ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(experiment_id, platform, commit_sha) DO UPDATE SET "
                    "run_id=excluded.run_id, status='inherited', unavailable_reason=excluded.unavailable_reason, "
                    "detail=excluded.detail, environment_json=excluded.environment_json, machine_id=excluded.machine_id, "
                    "inherited_from=excluded.inherited_from, result_json=excluded.result_json, "
                    "started_at=excluded.started_at, finished_at=excluded.finished_at",
                    (
                        experiment_id,
                        platform_id,
                        commit["sha"],
                        attempt["run_id"],
                        attempt.get("unavailable_reason"),
                        attempt.get("detail"),
                        attempt["environment_json"],
                        attempt.get("machine_id"),
                        source["sha"],
                        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                        attempt["started_at"],
                        attempt.get("finished_at"),
                    ),
                )
                written += 1
        return written

    def pending_uploads(self, experiment_id: str, platform_id: str) -> List[Dict[str, Any]]:
        """Terminal attempts the Worker has not acknowledged, sources before inherited copies."""
        rows = self.connection.execute(
            "SELECT a.*, c.ordinal FROM attempts a JOIN commits c ON c.sha=a.commit_sha "
            "WHERE experiment_id=? AND platform=? AND status != 'running' AND result_json IS NOT NULL AND uploaded_at IS NULL "
            "ORDER BY (inherited_from IS NOT NULL), finished_at, c.ordinal",
            (experiment_id, platform_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_uploaded(self, experiment_id: str, platform_id: str, commit_sha: str, uploaded_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE attempts SET uploaded_at=? WHERE experiment_id=? AND platform=? AND commit_sha=?",
                (uploaded_at, experiment_id, platform_id, commit_sha),
            )

    def record_adhoc(self, report: Dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO adhoc_runs(id, created_at, a_label, b_label, report_json) VALUES (?, ?, ?, ?, ?)",
                (
                    report["id"],
                    report["created_at"],
                    report["sides"][0]["label"],
                    report["sides"][1]["label"],
                    json.dumps(report, sort_keys=True, separators=(",", ":")),
                ),
            )

    def adhoc_runs(self) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT id, created_at, a_label, b_label FROM adhoc_runs ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def forget(self, experiment_id: str, platform_id: str, commit_sha: str) -> bool:
        """Drop a commit's result together with every result inherited from it."""
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM attempts WHERE experiment_id=? AND platform=? AND (commit_sha=? OR inherited_from=?)",
                (experiment_id, platform_id, commit_sha, commit_sha),
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
