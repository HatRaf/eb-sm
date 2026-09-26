import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta

import pytest
from helpers import KEY, ROOT, TIP, finals, fixture, obs

from ebasket.clock import FakeClock
from ebasket.ledger import Ledger, LedgerCorrupt, LedgerError, StaleState
from ebasket.model import EventKind, MatchKey, Status
from ebasket.states import EventState, IllegalTransition, JobState

MIGRATIONS = ROOT / "src" / "ebasket" / "migrations"

ARTIFACTS = dict(
    caption="Τελικό: Ολυμπιακός 82-79 Παναθηναϊκός",
    image_path="/output/x.jpg",
    image_sha256="ab" * 32,
    template_id="sample-feed-4x5",
    template_version="0",
    renderer_version="0.1.0",
    asset_manifest_hash="cd" * 32,
)


@pytest.fixture
def clock():
    return FakeClock(TIP)


@pytest.fixture
def ledger(tmp_path, clock):
    led = Ledger(tmp_path / "ledger.sqlite", clock)
    yield led
    led.close()


def confirmed_event(ledger, fx=None):
    fx = fx or fixture()
    match_id = ledger.upsert_match(fx).match_id
    obs_id = ledger.record_observation(match_id, finals(120)[0])
    event = ledger.ensure_event(match_id, EventKind.FINAL)
    return ledger.transition_event(event.id, EventState.CONFIRMED, actor="test", observation_id=obs_id)


def job_in(ledger, state, fmt="feed-4x5"):
    event = confirmed_event(ledger)
    job, _ = ledger.create_job(event.id, "noop", fmt)
    path = [JobState.RENDERED, JobState.VALIDATED, JobState.READY, JobState.PUBLISHING]
    for step in path[: path.index(state) + 1] if state in path else []:
        extra = ARTIFACTS if step is JobState.RENDERED else {}
        job = ledger.transition_job(job.job_key, step, actor="test", **extra)
    return job


# -- schema ---------------------------------------------------------------------


def test_migrates_fresh_db_and_reopens_idempotently(tmp_path):
    path = tmp_path / "l.sqlite"
    Ledger(path).close()
    led = Ledger(path)
    assert led.schema_version() == 2
    led.close()


def test_corrupted_file_is_refused(tmp_path):
    path = tmp_path / "l.sqlite"
    path.write_bytes(b"this is not a sqlite database" * 200)
    with pytest.raises(LedgerCorrupt):
        Ledger(path)


def test_v1_job_keys_are_migrated_to_source_qualified_keys(tmp_path):
    path = tmp_path / "v1.sqlite"
    old = "EUROLEAGUE:2026-27:E2026_7:FINAL:noop:feed-4x5"
    conn = sqlite3.connect(path)
    conn.executescript((MIGRATIONS / "0001_initial.sql").read_text(encoding="utf-8") + "PRAGMA user_version = 1;")
    conn.executescript(
        f"""
        INSERT INTO matches VALUES (1, 'EUROLEAGUE', '2026-27', 'euroleague-incrowd', 'E2026_7',
            '2026-09-24T18:15:00+00:00', 'PAN', 'PRS', 'panathinaikos', 'paris', 't', 't');
        INSERT INTO events (id, match_id, kind, state, created_utc, updated_utc)
            VALUES (1, 1, 'FINAL', 'confirmed', 't', 't');
        INSERT INTO jobs (job_key, natural_key, event_id, platform, format, state, created_utc, updated_utc)
            VALUES ('{old}', 'n', 1, 'noop', 'feed-4x5', 'ready', 't', 't');
        INSERT INTO attempts (job_key, started_utc) VALUES ('{old}', 't');
        INSERT INTO transitions (entity, entity_key, to_state, actor, at_utc) VALUES ('job', '{old}', 'ready', 'x', 't');
        """
    )
    conn.close()

    led = Ledger(path)
    new = "EUROLEAGUE:2026-27:euroleague-incrowd:E2026_7:FINAL:noop:feed-4x5"
    assert led.schema_version() == 2
    assert led.job(old) is None and led.job(new).state is JobState.READY
    assert led.conn.execute("SELECT job_key FROM attempts").fetchone()[0] == new
    assert [r["entity_key"] for r in led.history("job", new)] == [new]
    assert led.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    led.close()


# -- matches / observations -----------------------------------------------------


def test_upsert_match_is_idempotent(ledger):
    first = ledger.upsert_match(fixture())
    again = ledger.upsert_match(fixture())
    assert first.created and not again.created and again.match_id == first.match_id
    assert again.staged == ()


def test_swapped_teams_are_staged_and_nothing_is_overwritten(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    swapped = replace(
        fixture(), home_team_ref="PAN", away_team_ref="OLY", home_team_id="panathinaikos", away_team_id="olympiacos"
    )
    result = ledger.upsert_match(swapped)
    assert "teams" in result.staged
    assert ledger.fixture(match_id) == fixture()  # refs, canonical IDs and start all untouched
    assert ledger.pending_fixture_changes(match_id)


def test_team_change_holds_open_event_and_prepublish_job(ledger):
    job = job_in(ledger, JobState.READY)
    ledger.upsert_match(replace(fixture(), home_team_ref="RED", home_team_id="crvena-zvezda"))
    assert ledger.job(job.job_key).state is JobState.HELD
    assert ledger.job(job.job_key).reason == "fixture_changed"
    event = ledger.event(ledger.match_id(KEY), EventKind.FINAL)
    assert (event.state, event.reason) == (EventState.HELD, "fixture_changed")


def test_team_change_leaves_an_in_flight_publish_alone(ledger):
    job = job_in(ledger, JobState.PUBLISHING)
    ledger.upsert_match(replace(fixture(), home_team_ref="RED", home_team_id="crvena-zvezda"))
    assert ledger.job(job.job_key).state is JobState.PUBLISHING


def test_reschedule_before_confirmation_is_applied(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    ledger.ensure_event(match_id, EventKind.FINAL)
    later = TIP + timedelta(days=1)
    result = ledger.upsert_match(replace(fixture(), start_utc=later))
    assert result.start_changed and result.staged == ()
    assert ledger.fixture(match_id).start_utc == later


def test_reschedule_after_confirmation_is_staged_and_held(ledger):
    job = job_in(ledger, JobState.READY)
    result = ledger.upsert_match(replace(fixture(), start_utc=TIP + timedelta(days=1)))
    assert "start" in result.staged and not result.start_changed
    assert ledger.fixture(ledger.match_id(KEY)).start_utc == TIP
    assert ledger.job(job.job_key).state is JobState.HELD


def test_changed_canonical_id_is_staged(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    result = ledger.upsert_match(fixture(home_id="someone-else"))
    assert "team_ids" in result.staged
    assert ledger.fixture(match_id).home_team_id == "olympiacos"


def test_unmapped_ids_can_be_filled_in(ledger):
    match_id = ledger.upsert_match(fixture(home_id=None, away_id=None)).match_id
    result = ledger.upsert_match(fixture())
    assert result.staged == ()
    assert ledger.fixture(match_id) == fixture()


def test_observations_round_trip(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    history = [obs(60, current_period=3), *finals(120)]
    for o in history:
        ledger.record_observation(match_id, o)
    assert ledger.observations(match_id) == history


def test_observation_status_is_stored_verbatim(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    ledger.record_observation(match_id, obs(120, Status.UNKNOWN, raw_status="walkover"))
    (stored,) = ledger.observations(match_id)
    assert (stored.status, stored.raw_status) == (Status.UNKNOWN, "walkover")


# -- events ---------------------------------------------------------------------


def test_ensure_event_is_idempotent(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    a = ledger.ensure_event(match_id, EventKind.FINAL)
    b = ledger.ensure_event(match_id, EventKind.FINAL)
    assert a == b and a.state is EventState.OBSERVED
    assert len(ledger.history("event", str(a.id))) == 1


def test_event_transitions_are_enforced(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    event = ledger.ensure_event(match_id, EventKind.FINAL)
    held = ledger.transition_event(event.id, EventState.HELD, actor="test", reason="source_stale")
    with pytest.raises(IllegalTransition):
        ledger.transition_event(held.id, EventState.CONFIRMED, actor="test")
    retried = ledger.transition_event(held.id, EventState.OBSERVED, actor="cli:elena")
    assert retried.state is EventState.OBSERVED


# -- jobs: identity and duplicates ----------------------------------------------


def test_job_needs_confirmed_event(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    event = ledger.ensure_event(match_id, EventKind.FINAL)
    with pytest.raises(LedgerError):
        ledger.create_job(event.id, "noop", "feed-4x5")


def test_job_rereads_event_state(ledger):
    event = confirmed_event(ledger)  # caller holds a stale "confirmed" row...
    ledger.transition_event(event.id, EventState.HELD, actor="test", reason="result_changed")
    with pytest.raises(LedgerError):  # ...but the ledger checks the current state
        ledger.create_job(event.id, "noop", "feed-4x5")


def test_same_job_key_is_created_once(ledger):
    event = confirmed_event(ledger)
    job, created = ledger.create_job(event.id, "noop", "feed-4x5")
    again, created_again = ledger.create_job(event.id, "noop", "feed-4x5")
    assert created and not created_again and again == job
    assert job.job_key == "EUROLEAGUE:2026-27:test-source:E2026_42:FINAL:noop:feed-4x5"
    assert len(ledger.jobs()) == 1


def test_same_match_id_from_two_sources_are_two_games(ledger):
    ledger.create_job(confirmed_event(ledger).id, "noop", "feed-4x5")
    other = replace(
        fixture(home_id="real-madrid", away_id="barcelona"),
        key=replace(KEY, source="other-source"),
        home_team_ref="MAD",
        away_team_ref="BAR",
    )
    event2 = confirmed_event(ledger, other)
    job2, created = ledger.create_job(event2.id, "noop", "feed-4x5")
    assert created and job2.state is JobState.CONFIRMED
    assert len(ledger.jobs()) == 2


def test_job_key_owned_by_another_event_is_refused(ledger):
    event = confirmed_event(ledger)
    ledger.conn.execute(  # simulate a corrupted/foreign row already holding this key
        "INSERT INTO events (id, match_id, kind, state, created_utc, updated_utc)"
        " VALUES (99, ?, 'HALFTIME', 'confirmed', 't', 't')",
        (event.match_id,),
    )
    ledger.conn.execute(
        "INSERT INTO jobs (job_key, natural_key, event_id, platform, format, state, created_utc, updated_utc)"
        " VALUES ('EUROLEAGUE:2026-27:test-source:E2026_42:FINAL:noop:feed-4x5', 'n', 99, 'noop', 'feed-4x5',"
        " 'confirmed', 't', 't')"
    )
    with pytest.raises(LedgerError, match="already belongs"):
        ledger.create_job(event.id, "noop", "feed-4x5")


def test_same_game_from_another_source_is_held_as_duplicate(ledger):
    event = confirmed_event(ledger)
    ledger.create_job(event.id, "noop", "feed-4x5")
    same_game = replace(fixture(), key=MatchKey(KEY.competition, KEY.season, "other-source", "999"))
    event2 = confirmed_event(ledger, same_game)
    twin, created = ledger.create_job(event2.id, "noop", "feed-4x5")
    assert created and twin.state is JobState.HELD and twin.reason == "possible_duplicate"


def test_different_destination_is_not_a_duplicate(ledger):
    event = confirmed_event(ledger)
    ledger.create_job(event.id, "noop", "feed-4x5")
    story, _ = ledger.create_job(event.id, "noop", "story-9x16")
    assert story.state is JobState.CONFIRMED


# -- jobs: state machine --------------------------------------------------------


def test_rendered_requires_render_artifacts(ledger):
    job = job_in(ledger, JobState.CONFIRMED)
    with pytest.raises(LedgerError, match="render"):
        ledger.transition_job(job.job_key, JobState.RENDERED, actor="test")
    partial = {k: v for k, v in ARTIFACTS.items() if k != "image_sha256"}
    with pytest.raises(LedgerError, match="image_sha256"):
        ledger.transition_job(job.job_key, JobState.RENDERED, actor="test", **partial)


def test_retry_must_render_again(ledger):
    job = job_in(ledger, JobState.READY)
    ledger.transition_job(job.job_key, JobState.HELD, actor="worker", reason="asset_missing")
    ledger.transition_job(job.job_key, JobState.CONFIRMED, actor="cli:elena")
    with pytest.raises(LedgerError):  # old artifacts don't carry over into a new render
        ledger.transition_job(job.job_key, JobState.RENDERED, actor="worker")


def test_happy_path_to_published_requires_post_id(ledger):
    job = job_in(ledger, JobState.PUBLISHING)
    with pytest.raises(LedgerError):
        ledger.transition_job(job.job_key, JobState.PUBLISHED, actor="worker")
    job = ledger.transition_job(job.job_key, JobState.PUBLISHED, actor="worker", platform_post_id="178")
    assert job.state is JobState.PUBLISHED and job.published_utc is not None
    with pytest.raises(IllegalTransition):
        ledger.transition_job(job.job_key, JobState.HELD, actor="worker")


def test_cannot_skip_validation(ledger):
    job = job_in(ledger, JobState.CONFIRMED)
    with pytest.raises(IllegalTransition):
        ledger.transition_job(job.job_key, JobState.READY, actor="worker")


def test_publish_unknown_cannot_go_back_to_publishing(ledger):
    job = job_in(ledger, JobState.PUBLISHING)
    ledger.transition_job(job.job_key, JobState.PUBLISH_UNKNOWN, actor="worker")
    for target in (JobState.PUBLISHING, JobState.READY, JobState.CONFIRMED):
        with pytest.raises(IllegalTransition):
            ledger.transition_job(job.job_key, target, actor="worker")


def test_compare_and_swap_rejects_stale_expectation(ledger):
    job = job_in(ledger, JobState.READY)
    with pytest.raises(StaleState):
        ledger.transition_job(job.job_key, JobState.HELD, actor="worker", expected=JobState.CONFIRMED)


def test_unknown_job_field_is_rejected(ledger):
    job = job_in(ledger, JobState.READY)
    with pytest.raises(LedgerError):
        ledger.transition_job(job.job_key, JobState.HELD, actor="worker", access_token="nope")


def test_shadowed_is_terminal(ledger):
    job = job_in(ledger, JobState.READY)
    job = ledger.transition_job(job.job_key, JobState.SHADOWED, actor="worker")
    with pytest.raises(IllegalTransition):
        ledger.transition_job(job.job_key, JobState.PUBLISHING, actor="worker")


def test_transitions_are_audited(ledger):
    job = job_in(ledger, JobState.READY)
    ledger.transition_job(job.job_key, JobState.HELD, actor="worker", reason="asset_missing", detail="logo")
    rows = ledger.history("job", job.job_key)
    assert [r["to_state"] for r in rows] == ["confirmed", "rendered", "validated", "ready", "held"]
    assert (rows[-1]["reason"], rows[-1]["actor"]) == ("asset_missing", "worker")


# -- restart and crash handling -------------------------------------------------


def test_restart_turns_publishing_into_publish_unknown(tmp_path, clock):
    path = tmp_path / "l.sqlite"
    led = Ledger(path, clock)
    job = job_in(led, JobState.PUBLISHING)
    led.close()  # worker dies mid-publish

    led = Ledger(path, clock)
    assert led.job(job.job_key).state is JobState.PUBLISHING  # opening alone changes nothing
    assert led.recover_after_restart() == [job.job_key]
    assert led.job(job.job_key).state is JobState.PUBLISH_UNKNOWN
    assert led.recover_after_restart() == []
    led.close()


_CRASH = """
import os, sys
from ebasket.ledger import Ledger
led = Ledger(sys.argv[1])
led.conn.execute("BEGIN IMMEDIATE")
led.conn.execute(
    "INSERT INTO control (key, value, updated_utc) VALUES ('crash', 'uncommitted', 'x')")
os._exit(1)  # hard crash: no rollback, no close
"""


def test_uncommitted_write_is_lost_after_hard_crash(tmp_path):
    path = tmp_path / "l.sqlite"
    Ledger(path).close()
    proc = subprocess.run([sys.executable, "-c", _CRASH, str(path)])
    assert proc.returncode == 1
    led = Ledger(path)
    assert led.conn.execute("SELECT count(*) FROM control WHERE key='crash'").fetchone()[0] == 0
    led.close()


def test_committed_state_survives_reopen(tmp_path, clock):
    path = tmp_path / "l.sqlite"
    led = Ledger(path, clock)
    led.set_paused(True, "cli:test")
    led.beat()
    led.close()
    led = Ledger(path, clock)
    assert led.is_paused() and led.last_heartbeat() == TIP
    led.close()


def test_online_backup_restores(ledger, tmp_path):
    job = job_in(ledger, JobState.READY)
    dest = tmp_path / "backup.sqlite"
    ledger.backup_to(dest)
    restored = Ledger(dest)
    assert restored.job(job.job_key).state is JobState.READY
    restored.close()


def test_pause_flag(ledger):
    assert not ledger.is_paused()
    ledger.set_paused(True, "cli:test")
    assert ledger.is_paused()
    ledger.set_paused(False, "cli:test")
    assert not ledger.is_paused()
