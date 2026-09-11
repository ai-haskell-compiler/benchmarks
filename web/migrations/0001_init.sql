-- Migration number: 0001 	 2026-09-10T00:00:00.000Z
-- Index of published benchmark results. Raw envelopes live in R2; this
-- database holds only estimates and the identities needed to query them.

CREATE TABLE machines (
  machine_id   TEXT PRIMARY KEY,
  token_hash   TEXT NOT NULL,
  display_name TEXT,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT
);

CREATE TABLE environments (
  environment_id TEXT PRIMARY KEY,
  machine_id     TEXT NOT NULL REFERENCES machines(machine_id),
  first_seen_at  TEXT NOT NULL,
  record         TEXT NOT NULL
);

CREATE TABLE commits (
  sha          TEXT PRIMARY KEY,
  ordinal      INTEGER NOT NULL,
  committed_at TEXT NOT NULL,
  subject      TEXT NOT NULL,
  tree_key     TEXT
);
CREATE UNIQUE INDEX commits_ordinal ON commits(ordinal);

CREATE TABLE runs (
  run_id          TEXT PRIMARY KEY,
  machine_id      TEXT NOT NULL REFERENCES machines(machine_id),
  environment_id  TEXT NOT NULL,
  experiment_id   TEXT NOT NULL,
  commit_sha      TEXT NOT NULL,
  commit_ordinal  INTEGER NOT NULL,
  compiler_status TEXT NOT NULL,
  unavailable_reason TEXT,
  inherited_from  TEXT,
  created_at      TEXT NOT NULL,
  uploaded_at     TEXT NOT NULL,
  envelope_key    TEXT NOT NULL
);
CREATE INDEX runs_machine_ordinal ON runs(machine_id, experiment_id, commit_ordinal);
CREATE INDEX runs_commit ON runs(commit_sha);

CREATE TABLE measurements (
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  machine_id     TEXT NOT NULL,
  experiment_id  TEXT NOT NULL,
  commit_ordinal INTEGER NOT NULL,
  benchmark      TEXT NOT NULL,
  configuration  TEXT NOT NULL,
  compiler_family TEXT NOT NULL,
  compiler_version TEXT NOT NULL DEFAULT '',
  backend        TEXT NOT NULL,
  optimization   TEXT NOT NULL,
  baseline       INTEGER NOT NULL DEFAULT 0,
  metric         TEXT NOT NULL,
  unit           TEXT NOT NULL,
  status         TEXT NOT NULL,
  estimate       REAL,
  sample_count   INTEGER,
  PRIMARY KEY (machine_id, experiment_id, commit_ordinal, benchmark, configuration, metric)
);
CREATE INDEX measurements_series
  ON measurements(machine_id, experiment_id, benchmark, metric, optimization, commit_ordinal);
