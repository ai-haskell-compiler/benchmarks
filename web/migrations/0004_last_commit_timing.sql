-- Migration number: 0004 	 2026-09-28T00:00:00.000Z
-- How long each machine's most recently measured commit took, shown beside
-- the last upload on the overview. One value per machine: the uploader
-- replaces it only with a commit whose envelope is newer, so a backlog
-- uploaded out of order cannot put an older commit back.
--
-- `last_commit_timing` is the envelope's timing record as JSON:
-- {"total_ns": ..., "phases_ns": {"compiler_build": ..., ...}}.

ALTER TABLE machines ADD COLUMN last_commit_sha TEXT;
ALTER TABLE machines ADD COLUMN last_commit_at TEXT;
ALTER TABLE machines ADD COLUMN last_commit_timing TEXT;
