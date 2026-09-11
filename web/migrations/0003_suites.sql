-- Migration number: 0003 	 2026-09-11T00:00:00.000Z
-- Experiments are now per benchmark, so the site needs to know which
-- experiments make up the suite it shows. The uploader records the suite it
-- ran with here; the most recently uploaded suite is the active one and
-- `experiments` maps benchmark id to experiment id.
--
-- Earlier uploads used one experiment for the whole suite. Each of those
-- becomes a suite of its own so the site keeps showing them until the first
-- per-benchmark upload arrives.

CREATE TABLE suites (
  suite_key   TEXT PRIMARY KEY,
  suite_id    TEXT NOT NULL,
  experiments TEXT NOT NULL,
  uploaded_at TEXT NOT NULL
);

INSERT INTO suites(suite_key, suite_id, experiments, uploaded_at)
SELECT r.experiment_id, r.experiment_id,
  COALESCE(
    (SELECT json_group_object(b.benchmark, b.experiment_id)
       FROM (SELECT DISTINCT experiment_id, benchmark FROM measurements WHERE experiment_id = r.experiment_id) b),
    '{}'),
  MAX(r.uploaded_at)
FROM runs r GROUP BY r.experiment_id;
