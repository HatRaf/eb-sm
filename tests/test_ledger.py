import subprocess
import sys
from dataclasses import replace
from datetime import timedelta

import pytest
from helpers import KEY, TIP, finals, fixture, obs

from ebasket.clock import FakeClock
from ebasket.ledger import Ledger, LedgerCorrupt, LedgerError, StaleState
from ebasket.model import EventKind, MatchKey, Status
from ebasket.states import EventState, IllegalTransition, JobState


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
    event = ledger.transition_event(event.id, EventState.CONFIRMED, actor="test", observation_id=obs_id)
    return fx, event


def ready_job(ledger):
    fx, event = confirmed_event(ledger)
    job, _ = ledger.create_job(event, fx, "noop", "feed-4x5")
    for state in (JobState.RENDERED, JobState.VALIDATED, JobState.READY):
        job = ledger.transition_job(job.job_key, state, actor="test")
    return job


# -- schema ---------------------------------------------------------------------


def test_migrates_fresh_db_and_reopens_idempotently(tmp_path):
    path = tmp_path / "l.sqlite"
    Ledger(path).close()
    led = Ledger(path)
    assert led.schema_version() == 1
    led.close()


def test_corrupted_file_is_refused(tmp_path):
    path = tmp_path / "l.sqlite"
    path.write_bytes(b"this is not a sqlite database" * 200)
    with pytest.raises(LedgerCorrupt):
        Ledger(path)


# -- matches / observations -----------------------------------------------------


def test_upsert_match_is_idempotent_and_flags_changes(ledger):
    first = ledger.upsert_match(fixture())
    again = ledger.upsert_match(fixture())
    assert first.created and not again.created and again.match_id == first.match_id

    moved = ledger.upsert_match(replace(fixture(), start_utc=TIP + timedelta(hours=1)))
    assert moved.start_changed and not moved.teams_changed

    swapped = ledger.upsert_match(replace(fixture(), home_team_ref="PAN", away_team_ref="OLY"))
    assert swapped.teams_changed
    # team refs are never silently overwritten
    assert ledger.fixture(first.match_id).home_team_ref == "OLY"


def test_observations_round_trip(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    history = [obs(60, current_period=3), *finals(120)]
    for o in history:
        ledger.record_observation(match_id, o)
    assert ledger.observations(match_id) == history


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


# -- jobs: duplicates -----------------------------------------------------------


def test_job_needs_confirmed_event(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    event = ledger.ensure_event(match_id, EventKind.FINAL)
    with pytest.raises(LedgerError):
        ledger.create_job(event, fixture(), "noop", "feed-4x5")


def test_same_job_key_is_created_once(ledger):
    fx, event = confirmed_event(ledger)
    job, created = ledger.create_job(event, fx, "noop", "feed-4x5")
    again, created_again = ledger.create_job(event, fx, "noop", "feed-4x5")
    assert created and not created_again and again == job
    assert job.job_key == "EUROLEAGUE:2026-27:E2026_42:FINAL:noop:feed-4x5"
    assert len(ledger.jobs()) == 1


def test_same_game_from_another_source_is_held_as_duplicate(ledger):
    fx, event = confirmed_event(ledger)
    ledger.create_job(event, fx, "noop", "feed-4x5")

    other_source = replace(fixture(), key=MatchKey(KEY.competition, KEY.season, "other-source", "999"))
    fx2, event2 = confirmed_event(ledger, other_source)
    twin, created = ledger.create_job(event2, fx2, "noop", "feed-4x5")
    assert created and twin.state is JobState.HELD and twin.reason == "possible_duplicate"


def test_different_destination_is_not_a_duplicate(ledger):
    fx, event = confirmed_event(ledger)
    ledger.create_job(event, fx, "noop", "feed-4x5")
    story, _ = ledger.create_job(event, fx, "noop", "story-9x16")
    assert story.state is JobState.CONFIRMED


# -- jobs: state machine --------------------------------------------------------


def test_happy_path_to_published_requires_post_id(ledger):
    job = ready_job(ledger)
    job = ledger.transition_job(job.job_key, JobState.PUBLISHING, actor="worker")
    with pytest.raises(LedgerError):
        ledger.transition_job(job.job_key, JobState.PUBLISHED, actor="worker")
    job = ledger.transition_job(job.job_key, JobState.PUBLISHED, actor="worker", platform_post_id="178")
    assert job.state is JobState.PUBLISHED and job.published_utc is not None
    with pytest.raises(IllegalTransition):
        ledger.transition_job(job.job_key, JobState.HELD, actor="worker")


def test_cannot_skip_validation(ledger):
    fx, event = confirmed_event(ledger)
    job, _ = ledger.create_job(event, fx, "noop", "feed-4x5")
    with pytest.raises(IllegalTransition):
        ledger.transition_job(job.job_key, JobState.READY, actor="worker")


def test_publish_unknown_cannot_go_back_to_publishing(ledger):
    job = ready_job(ledger)
    ledger.transition_job(job.job_key, JobState.PUBLISHING, actor="worker")
    ledger.transition_job(job.job_key, JobState.PUBLISH_UNKNOWN, actor="worker")
    for target in (JobState.PUBLISHING, JobState.READY, JobState.CONFIRMED):
        with pytest.raises(IllegalTransition):
            ledger.transition_job(job.job_key, target, actor="worker")


def test_compare_and_swap_rejects_stale_expectation(ledger):
    job = ready_job(ledger)
    with pytest.raises(StaleState):
        ledger.transition_job(job.job_key, JobState.HELD, actor="worker", expected=JobState.CONFIRMED)


def test_unknown_job_field_is_rejected(ledger):
    job = ready_job(ledger)
    with pytest.raises(LedgerError):
        ledger.transition_job(job.job_key, JobState.HELD, actor="worker", access_token="nope")


def test_shadowed_is_terminal(ledger):
    job = ready_job(ledger)
    job = ledger.transition_job(job.job_key, JobState.SHADOWED, actor="worker")
    with pytest.raises(IllegalTransition):
        ledger.transition_job(job.job_key, JobState.PUBLISHING, actor="worker")


def test_transitions_are_audited(ledger):
    job = ready_job(ledger)
    ledger.transition_job(job.job_key, JobState.HELD, actor="worker", reason="asset_missing", detail="logo")
    rows = ledger.history("job", job.job_key)
    assert [r["to_state"] for r in rows] == ["confirmed", "rendered", "validated", "ready", "held"]
    assert (rows[-1]["reason"], rows[-1]["actor"]) == ("asset_missing", "worker")


# -- restart and crash recovery -------------------------------------------------


def test_restart_turns_publishing_into_publish_unknown(tmp_path, clock):
    path = tmp_path / "l.sqlite"
    led = Ledger(path, clock)
    job = ready_job(led)
    led.transition_job(job.job_key, JobState.PUBLISHING, actor="worker")
    led.close()  # worker dies mid-publish

    led = Ledger(path, clock)
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
    job = ready_job(ledger)
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


def test_observation_of_final_status_is_stored_verbatim(ledger):
    match_id = ledger.upsert_match(fixture()).match_id
    ledger.record_observation(match_id, obs(120, Status.UNKNOWN, raw_status="walkover"))
    (stored,) = ledger.observations(match_id)
    assert (stored.status, stored.raw_status) == (Status.UNKNOWN, "walkover")
