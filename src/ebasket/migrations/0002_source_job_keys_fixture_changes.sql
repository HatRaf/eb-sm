-- Ledger schema v2.
-- 1. Job keys gain the source (competition:season:source:source_match_id:event:platform:format)
--    so two providers' match IDs can never collide. Existing keys are rewritten everywhere they
--    are referenced; foreign keys are checked at commit.
-- 2. fixture_changes stages source-reported fixture changes that must not be applied silently.

PRAGMA defer_foreign_keys = ON;

CREATE TEMP TABLE job_key_map AS
SELECT j.job_key AS old_key,
       m.competition || ':' || m.season || ':' || m.source || ':' || m.source_match_id || ':' ||
       e.kind || ':' || j.platform || ':' || j.format AS new_key
FROM jobs j
JOIN events e ON e.id = j.event_id
JOIN matches m ON m.id = e.match_id;

UPDATE jobs SET job_key = (SELECT new_key FROM job_key_map WHERE old_key = jobs.job_key);
UPDATE attempts SET job_key = (SELECT new_key FROM job_key_map WHERE old_key = attempts.job_key);
UPDATE transitions
   SET entity_key = (SELECT new_key FROM job_key_map WHERE old_key = transitions.entity_key)
 WHERE entity = 'job' AND entity_key IN (SELECT old_key FROM job_key_map);

DROP TABLE job_key_map;

CREATE TABLE fixture_changes (
    id           INTEGER PRIMARY KEY,
    match_id     INTEGER NOT NULL REFERENCES matches (id),
    field        TEXT NOT NULL CHECK (field IN ('teams', 'team_ids', 'start')),
    old_value    TEXT NOT NULL,
    new_value    TEXT NOT NULL,
    detected_utc TEXT NOT NULL,
    resolved_utc TEXT,
    resolved_by  TEXT
);
CREATE UNIQUE INDEX fixture_changes_open
    ON fixture_changes (match_id, field, new_value) WHERE resolved_utc IS NULL;
