-- Ledger schema v1. Timestamps are ISO-8601 UTC strings.

CREATE TABLE matches (
    id              INTEGER PRIMARY KEY,
    competition     TEXT NOT NULL,
    season          TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    start_utc       TEXT NOT NULL,
    home_team_ref   TEXT NOT NULL,
    away_team_ref   TEXT NOT NULL,
    home_team_id    TEXT,
    away_team_id    TEXT,
    created_utc     TEXT NOT NULL,
    updated_utc     TEXT NOT NULL,
    UNIQUE (competition, season, source, source_match_id)
);
CREATE INDEX matches_start ON matches (start_utc);

CREATE TABLE observations (
    id                 INTEGER PRIMARY KEY,
    match_id           INTEGER NOT NULL REFERENCES matches (id),
    fetched_utc        TEXT NOT NULL,
    source_asof_utc    TEXT,
    status             TEXT NOT NULL,
    raw_status         TEXT NOT NULL,
    home_team_ref      TEXT NOT NULL,
    away_team_ref      TEXT NOT NULL,
    home_score         INTEGER,
    away_score         INTEGER,
    period_scores_json TEXT NOT NULL,
    current_period     INTEGER,
    start_utc          TEXT NOT NULL,
    raw_sha256         TEXT NOT NULL
);
CREATE INDEX observations_match ON observations (match_id, fetched_utc);

CREATE TABLE events (
    id             INTEGER PRIMARY KEY,
    match_id       INTEGER NOT NULL REFERENCES matches (id),
    kind           TEXT NOT NULL CHECK (kind IN ('HALFTIME', 'FINAL')),
    state          TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    detail         TEXT NOT NULL DEFAULT '',
    observation_id INTEGER REFERENCES observations (id),  -- confirmed snapshot
    created_utc    TEXT NOT NULL,
    updated_utc    TEXT NOT NULL,
    UNIQUE (match_id, kind)
);

CREATE TABLE jobs (
    job_key             TEXT PRIMARY KEY,
    natural_key         TEXT NOT NULL,
    event_id            INTEGER NOT NULL REFERENCES events (id),
    platform            TEXT NOT NULL,
    format              TEXT NOT NULL,
    state               TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    detail              TEXT NOT NULL DEFAULT '',
    caption             TEXT,
    image_path          TEXT,
    image_sha256        TEXT,
    template_id         TEXT,
    template_version    TEXT,
    renderer_version    TEXT,
    asset_manifest_hash TEXT,
    platform_post_id    TEXT,
    created_utc         TEXT NOT NULL,
    updated_utc         TEXT NOT NULL,
    published_utc       TEXT
);
CREATE INDEX jobs_natural_key ON jobs (natural_key);
CREATE INDEX jobs_state ON jobs (state);

CREATE TABLE attempts (
    id           INTEGER PRIMARY KEY,
    job_key      TEXT NOT NULL REFERENCES jobs (job_key),
    started_utc  TEXT NOT NULL,
    finished_utc TEXT,
    outcome      TEXT,
    http_status  INTEGER,
    detail       TEXT NOT NULL DEFAULT ''  -- redacted: never tokens
);

CREATE TABLE transitions (
    id         INTEGER PRIMARY KEY,
    entity     TEXT NOT NULL CHECK (entity IN ('event', 'job')),
    entity_key TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL,
    at_utc     TEXT NOT NULL
);
CREATE INDEX transitions_entity ON transitions (entity, entity_key);

CREATE TABLE notifications (
    id          INTEGER PRIMARY KEY,
    dedupe_key  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    sent_utc    TEXT
);

CREATE TABLE control (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);
