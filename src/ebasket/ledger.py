"""SQLite ledger: the durable state machine and audit trail.

Every transition is committed (synchronous=FULL) before the side effect it
guards — a job is `publishing` on disk before any platform call is made.
State changes are compare-and-swap on the current state, so two actors can't
both move the same job.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

from .clock import Clock, SystemClock
from .model import Competition, EventKind, Fixture, MatchKey, Observation, Status
from .states import (
    EventState,
    JobState,
    check_event_transition,
    check_job_transition,
    job_key,
    natural_key,
)


class LedgerError(Exception):
    pass


class LedgerCorrupt(LedgerError):
    pass


class StaleState(LedgerError):
    """The row changed under us: the compare-and-swap on its state failed."""


# Set together, and only, on the transition to `rendered`; required from then on.
RENDER_FIELDS = (
    "caption",
    "image_path",
    "image_sha256",
    "template_id",
    "template_version",
    "renderer_version",
    "asset_manifest_hash",
)
JOB_FIELDS = frozenset({*RENDER_FIELDS, "platform_post_id"})
_NEEDS_RENDER = frozenset({JobState.VALIDATED, JobState.READY, JobState.PUBLISHING})
_PRE_PUBLISH = (JobState.CONFIRMED, JobState.RENDERED, JobState.VALIDATED, JobState.READY)


@dataclass(frozen=True)
class FixtureUpsert:
    match_id: int
    created: bool
    start_changed: bool  # a safe reschedule that was applied
    staged: tuple[str, ...] = ()  # changes NOT applied: stored for a person, match held


@dataclass(frozen=True)
class EventRow:
    id: int
    match_id: int
    kind: EventKind
    state: EventState
    reason: str
    detail: str
    observation_id: int | None


@dataclass(frozen=True)
class JobRow:
    job_key: str
    natural_key: str
    event_id: int
    platform: str
    format: str
    state: JobState
    reason: str
    detail: str
    caption: str | None
    image_path: str | None
    image_sha256: str | None
    template_id: str | None
    template_version: str | None
    renderer_version: str | None
    asset_manifest_hash: str | None
    platform_post_id: str | None
    published_utc: datetime | None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class Ledger:
    def __init__(self, path: str | Path, clock: Clock | None = None) -> None:
        self.path = str(path)
        self.clock = clock or SystemClock()
        self._depth = 0
        try:
            self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            result = self.conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise LedgerCorrupt(f"{self.path}: {exc}") from exc
        if result != "ok":
            raise LedgerCorrupt(f"{self.path}: integrity check failed: {result}")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # -- plumbing ---------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        current = self.schema_version()
        folder = resources.files("ebasket").joinpath("migrations")
        scripts = sorted(
            (int(m.group(1)), entry)
            for entry in folder.iterdir()
            if (m := re.fullmatch(r"(\d+)_.*\.sql", entry.name))
        )
        for number, entry in scripts:
            if number <= current:
                continue
            sql = entry.read_text(encoding="utf-8")
            try:
                self.conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
            except sqlite3.Error:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            current = number
        return current

    def _now(self) -> str:
        return self.clock.now().isoformat()

    def _log(self, entity: str, key: str, from_state: str | None, to_state: str, reason: str, detail: str, actor: str) -> None:
        self.conn.execute(
            "INSERT INTO transitions (entity, entity_key, from_state, to_state, reason, detail, actor, at_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entity, key, from_state, to_state, reason, detail, actor, self._now()),
        )

    # -- matches and observations ----------------------------------------

    def upsert_match(self, fixture: Fixture, actor: str = "worker") -> FixtureUpsert:
        """Insert a fixture, or apply only safe updates to a known one.

        Safe: filling in canonical IDs that were unmapped; moving the start time while
        nothing has been confirmed or held. Anything else (other teams, replaced IDs,
        a reschedule after progress) is staged in fixture_changes, the row is left
        untouched, and the match's open events and pre-publish jobs are held — all in
        one transaction.
        """
        k = fixture.key
        now = self._now()
        with self.tx():
            match_id = self.match_id(k)
            if match_id is None:
                cur = self.conn.execute(
                    "INSERT INTO matches (competition, season, source, source_match_id, start_utc, home_team_ref,"
                    " away_team_ref, home_team_id, away_team_id, created_utc, updated_utc)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        k.competition, k.season, k.source, k.source_match_id, _iso(fixture.start_utc),
                        fixture.home_team_ref, fixture.away_team_ref, fixture.home_team_id,
                        fixture.away_team_id, now, now,
                    ),
                )
                return FixtureUpsert(cur.lastrowid, created=True, start_changed=False)

            stored = self.fixture(match_id)
            staged: list[tuple[str, str, str]] = []
            old_refs = f"{stored.home_team_ref}-{stored.away_team_ref}"
            new_refs = f"{fixture.home_team_ref}-{fixture.away_team_ref}"
            if old_refs != new_refs:
                staged.append(("teams", old_refs, new_refs))
            elif any(
                old is not None and new != old
                for old, new in (
                    (stored.home_team_id, fixture.home_team_id),
                    (stored.away_team_id, fixture.away_team_id),
                )
            ):
                staged.append(
                    (
                        "team_ids",
                        f"{stored.home_team_id}-{stored.away_team_id}",
                        f"{fixture.home_team_id}-{fixture.away_team_id}",
                    )
                )
            start_changed = stored.start_utc != fixture.start_utc
            if start_changed and self._has_progress(match_id):
                staged.append(("start", _iso(stored.start_utc), _iso(fixture.start_utc)))

            if staged:
                for field, old, new in staged:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO fixture_changes (match_id, field, old_value, new_value, detected_utc)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (match_id, field, old, new, now),
                    )
                detail = "; ".join(f"{field}: {old} -> {new}" for field, old, new in staged)
                self._hold_match(match_id, "fixture_changed", detail, actor)
                return FixtureUpsert(match_id, False, False, tuple(field for field, _, _ in staged))

            self.conn.execute(
                "UPDATE matches SET start_utc=?, home_team_id=?, away_team_id=?, updated_utc=? WHERE id=?",
                (
                    _iso(fixture.start_utc),
                    stored.home_team_id or fixture.home_team_id,
                    stored.away_team_id or fixture.away_team_id,
                    now,
                    match_id,
                ),
            )
            return FixtureUpsert(match_id, created=False, start_changed=start_changed)

    def _has_progress(self, match_id: int) -> bool:
        row = self.conn.execute(
            "SELECT (SELECT count(*) FROM events WHERE match_id=? AND state != ?)"
            " + (SELECT count(*) FROM jobs JOIN events ON events.id = jobs.event_id WHERE events.match_id=?)",
            (match_id, EventState.OBSERVED, match_id),
        ).fetchone()
        return row[0] > 0

    def _hold_match(self, match_id: int, reason: str, detail: str, actor: str) -> None:
        """Hold open events and jobs not yet handed to a publisher.

        In-flight (`publishing`, `publish_unknown`) and published jobs are left alone:
        taking them away from the publisher would lose the platform's answer.
        """
        with self.tx():
            events = self.conn.execute(
                "SELECT id FROM events WHERE match_id=? AND state IN (?, ?)",
                (match_id, EventState.OBSERVED, EventState.CONFIRMED),
            ).fetchall()
            for row in events:
                self.transition_event(row["id"], EventState.HELD, actor=actor, reason=reason, detail=detail)
            jobs = self.conn.execute(
                "SELECT jobs.job_key FROM jobs JOIN events ON events.id = jobs.event_id"
                f" WHERE events.match_id=? AND jobs.state IN ({', '.join('?' * len(_PRE_PUBLISH))})",
                (match_id, *_PRE_PUBLISH),
            ).fetchall()
            for row in jobs:
                self.transition_job(row["job_key"], JobState.HELD, actor=actor, reason=reason, detail=detail)

    def pending_fixture_changes(self, match_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM fixture_changes WHERE match_id=? AND resolved_utc IS NULL ORDER BY id", (match_id,)
            )
        )

    def match_id(self, key: MatchKey) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM matches WHERE competition=? AND season=? AND source=? AND source_match_id=?",
            (key.competition, key.season, key.source, key.source_match_id),
        ).fetchone()
        return None if row is None else row["id"]

    def fixture(self, match_id: int) -> Fixture:
        r = self.conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if r is None:
            raise LedgerError(f"no match {match_id}")
        return Fixture(
            MatchKey(Competition(r["competition"]), r["season"], r["source"], r["source_match_id"]),
            _dt(r["start_utc"]),
            r["home_team_ref"],
            r["away_team_ref"],
            r["home_team_id"],
            r["away_team_id"],
        )

    def matches_starting_between(self, start: datetime, end: datetime) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM matches WHERE start_utc >= ? AND start_utc < ? ORDER BY start_utc, id",
            (_iso(start), _iso(end)),
        )
        return [r["id"] for r in rows]

    def record_observation(self, match_id: int, obs: Observation) -> int:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO observations (match_id, fetched_utc, source_asof_utc, status, raw_status,"
                " home_team_ref, away_team_ref, home_score, away_score, period_scores_json, current_period,"
                " start_utc, raw_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match_id, _iso(obs.fetched_utc), _iso(obs.source_asof_utc), obs.status, obs.raw_status,
                    obs.home_team_ref, obs.away_team_ref, obs.home_score, obs.away_score,
                    json.dumps(obs.period_scores), obs.current_period, _iso(obs.start_utc), obs.raw_sha256,
                ),
            )
            return cur.lastrowid

    def observations(self, match_id: int) -> list[Observation]:
        key = self.fixture(match_id).key
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE match_id=? ORDER BY fetched_utc, id", (match_id,)
        )
        return [
            Observation(
                key=key,
                start_utc=_dt(r["start_utc"]),
                home_team_ref=r["home_team_ref"],
                away_team_ref=r["away_team_ref"],
                status=Status(r["status"]),
                raw_status=r["raw_status"],
                home_score=r["home_score"],
                away_score=r["away_score"],
                period_scores=tuple(tuple(p) for p in json.loads(r["period_scores_json"])),
                current_period=r["current_period"],
                source_asof_utc=_dt(r["source_asof_utc"]),
                fetched_utc=_dt(r["fetched_utc"]),
                raw_sha256=r["raw_sha256"],
            )
            for r in rows
        ]

    # -- events -------------------------------------------------------------

    def _event_row(self, r: sqlite3.Row) -> EventRow:
        return EventRow(
            r["id"], r["match_id"], EventKind(r["kind"]), EventState(r["state"]),
            r["reason"], r["detail"], r["observation_id"],
        )

    def event(self, match_id: int, kind: EventKind) -> EventRow | None:
        r = self.conn.execute("SELECT * FROM events WHERE match_id=? AND kind=?", (match_id, kind)).fetchone()
        return None if r is None else self._event_row(r)

    def ensure_event(self, match_id: int, kind: EventKind, actor: str = "worker") -> EventRow:
        now = self._now()
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO events (match_id, kind, state, created_utc, updated_utc) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (match_id, kind) DO NOTHING",
                (match_id, kind, EventState.OBSERVED, now, now),
            )
            row = self.event(match_id, kind)
            if cur.rowcount:
                self._log("event", str(row.id), None, EventState.OBSERVED, "", "", actor)
        return row

    def transition_event(
        self,
        event_id: int,
        to: EventState,
        *,
        actor: str,
        reason: str = "",
        detail: str = "",
        observation_id: int | None = None,
    ) -> EventRow:
        with self.tx():
            r = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if r is None:
                raise LedgerError(f"no event {event_id}")
            current = EventState(r["state"])
            check_event_transition(current, to)
            cur = self.conn.execute(
                "UPDATE events SET state=?, reason=?, detail=?, observation_id=COALESCE(?, observation_id),"
                " updated_utc=? WHERE id=? AND state=?",
                (to, reason, detail, observation_id, self._now(), event_id, current),
            )
            if cur.rowcount != 1:
                raise StaleState(f"event {event_id} is no longer {current}")
            self._log("event", str(event_id), current, to, reason, detail, actor)
            return self._event_row(self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    # -- jobs -----------------------------------------------------------------

    def _job_row(self, r: sqlite3.Row) -> JobRow:
        return JobRow(
            job_key=r["job_key"],
            natural_key=r["natural_key"],
            event_id=r["event_id"],
            platform=r["platform"],
            format=r["format"],
            state=JobState(r["state"]),
            reason=r["reason"],
            detail=r["detail"],
            platform_post_id=r["platform_post_id"],
            published_utc=_dt(r["published_utc"]),
            **{field: r[field] for field in RENDER_FIELDS},
        )

    def job(self, key: str) -> JobRow | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
        return None if r is None else self._job_row(r)

    def jobs(self, state: JobState | None = None) -> list[JobRow]:
        if state is None:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_utc, job_key")
        else:
            rows = self.conn.execute("SELECT * FROM jobs WHERE state=? ORDER BY created_utc, job_key", (state,))
        return [self._job_row(r) for r in rows]

    def create_job(self, event_id: int, platform: str, fmt: str, actor: str = "worker") -> tuple[JobRow, bool]:
        """Idempotent: returns (job, created).

        The event's current state and its match's stored fixture are read inside the
        transaction — nothing about the job's identity comes from the caller. A second
        job for the same real-world post, or one for a match with unresolved fixture
        changes, is created held.
        """
        now = self._now()
        with self.tx():
            r = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if r is None:
                raise LedgerError(f"no event {event_id}")
            event = self._event_row(r)
            if event.state is not EventState.CONFIRMED:
                raise LedgerError(f"event {event.id} is {event.state}, not confirmed")
            fixture = self.fixture(event.match_id)
            if fixture.home_team_id is None or fixture.away_team_id is None:
                raise LedgerError(f"event {event.id} is confirmed but its match has unmapped teams")
            key = job_key(fixture, event.kind, platform, fmt)
            nat = natural_key(fixture, event.kind, platform, fmt)
            existing = self.job(key)
            if existing is not None:
                if existing.event_id != event.id:
                    raise LedgerError(f"job key {key} already belongs to event {existing.event_id}")
                return existing, False
            twin = self.conn.execute(
                "SELECT job_key FROM jobs WHERE natural_key=? AND state != ?", (nat, JobState.DISMISSED)
            ).fetchone()
            state, reason, detail = JobState.CONFIRMED, "", ""
            if twin is not None:
                state, reason = JobState.HELD, "possible_duplicate"
                detail = f"same game and destination as {twin['job_key']}"
            elif self.pending_fixture_changes(event.match_id):
                state, reason = JobState.HELD, "fixture_changed"
                detail = "match has unresolved fixture changes"
            self.conn.execute(
                "INSERT INTO jobs (job_key, natural_key, event_id, platform, format, state, reason, detail,"
                " created_utc, updated_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, nat, event.id, platform, fmt, state, reason, detail, now, now),
            )
            self._log("job", key, None, state, reason, detail, actor)
            return self.job(key), True

    def transition_job(
        self,
        key: str,
        to: JobState,
        *,
        actor: str,
        reason: str = "",
        detail: str = "",
        expected: JobState | None = None,
        **fields: str,
    ) -> JobRow:
        unknown = set(fields) - JOB_FIELDS
        if unknown:
            raise LedgerError(f"unknown job fields: {sorted(unknown)}")
        with self.tx():
            job = self.job(key)
            if job is None:
                raise LedgerError(f"no job {key}")
            if expected is not None and job.state is not expected:
                raise StaleState(f"job {key} is {job.state}, expected {expected}")
            check_job_transition(job.state, to)
            self._check_render_fields(job, to, fields)
            if to is JobState.PUBLISHED and not (fields.get("platform_post_id") or job.platform_post_id):
                raise LedgerError("a job can only be marked published with the platform post ID")
            now = self._now()
            sets = {**fields, "state": to, "reason": reason, "detail": detail, "updated_utc": now}
            if to is JobState.CONFIRMED:  # a retry starts from scratch: old artifacts don't carry over
                sets.update(dict.fromkeys(RENDER_FIELDS))
            if to is JobState.PUBLISHED:
                sets["published_utc"] = now
            assignments = ", ".join(f"{column}=?" for column in sets)
            cur = self.conn.execute(
                f"UPDATE jobs SET {assignments} WHERE job_key=? AND state=?",
                (*sets.values(), key, job.state),
            )
            if cur.rowcount != 1:
                raise StaleState(f"job {key} is no longer {job.state}")
            self._log("job", key, job.state, to, reason, detail, actor)
            return self.job(key)

    @staticmethod
    def _check_render_fields(job: JobRow, to: JobState, fields: dict[str, str]) -> None:
        # A state name is not proof of content: the ledger itself refuses to move a job past
        # `rendered` without the artifacts, and refuses to swap artifacts after rendering.
        given = set(fields) & set(RENDER_FIELDS)
        if to is JobState.RENDERED:
            missing = [f for f in RENDER_FIELDS if not fields.get(f)]
            if missing:
                raise LedgerError(f"render artifacts missing: {', '.join(missing)}")
            if not re.fullmatch(r"[0-9a-f]{64}", fields["image_sha256"]):
                raise LedgerError("image_sha256 must be a lowercase hex SHA-256")
        elif given:
            raise LedgerError(f"render artifacts can only be set when rendering, not on {to}")
        elif to in _NEEDS_RENDER:
            missing = [f for f in RENDER_FIELDS if not getattr(job, f)]
            if missing:
                raise LedgerError(f"job {job.job_key} has no render artifacts ({', '.join(missing)})")

    def start_attempt(self, key: str) -> int:
        with self.tx():
            return self.conn.execute(
                "INSERT INTO attempts (job_key, started_utc) VALUES (?, ?)", (key, self._now())
            ).lastrowid

    def finish_attempt(self, attempt_id: int, outcome: str, http_status: int | None = None, detail: str = "") -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE attempts SET finished_utc=?, outcome=?, http_status=?, detail=? WHERE id=?",
                (self._now(), outcome, http_status, detail, attempt_id),
            )

    def recover_after_restart(self, actor: str = "worker:startup") -> list[str]:
        """A job left in `publishing` may or may not have reached the platform.

        Call only from worker start-up, before any publish or reconcile. Deliberately not
        run on open: `ebasket status` opening the ledger mid-publish must not disturb it.
        """
        recovered = []
        for job in self.jobs(JobState.PUBLISHING):
            self.transition_job(
                job.job_key,
                JobState.PUBLISH_UNKNOWN,
                actor=actor,
                reason="restart_during_publish",
                detail="worker stopped after the publish call started; reconcile before any retry",
                expected=JobState.PUBLISHING,
            )
            recovered.append(job.job_key)
        return recovered

    def history(self, entity: str, key: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM transitions WHERE entity=? AND entity_key=? ORDER BY id", (entity, key)
            )
        )

    # -- control ------------------------------------------------------------

    def _set_control(self, key: str, value: str) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO control (key, value, updated_utc) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_utc=excluded.updated_utc",
                (key, value, self._now()),
            )

    def _control(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT value, updated_utc FROM control WHERE key=?", (key,)).fetchone()

    def set_paused(self, paused: bool, actor: str) -> None:
        self._set_control("paused", json.dumps({"paused": paused, "by": actor}))

    def is_paused(self) -> bool:
        row = self._control("paused")
        return bool(row and json.loads(row["value"])["paused"])

    def beat(self) -> None:
        self._set_control("heartbeat", self._now())

    def last_heartbeat(self) -> datetime | None:
        row = self._control("heartbeat")
        return None if row is None else _dt(row["value"])

    # -- backup -------------------------------------------------------------

    def backup_to(self, dest: str | Path) -> None:
        """Online backup (consistent even while the worker writes); never a raw file copy."""
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()
