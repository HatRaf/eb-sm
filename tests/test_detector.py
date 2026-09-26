import random

import pytest
from helpers import HALF, REGULATION, TIED_REGULATION, KEY, at, finals, fixture, obs

from ebasket.detector import DetectorConfig, Outcome, Reason, detect
from ebasket.model import Competition, EventKind, MatchKey, Status

FINAL, HALFTIME = EventKind.FINAL, EventKind.HALFTIME


def decide(history, now_minute, kind=FINAL, fx=None, cfg=DetectorConfig()):
    return detect(kind, fx or fixture(), history, at(now_minute), cfg)


# -- final: confirmation window -------------------------------------------------


def test_final_confirmed_after_three_observations_over_90s():
    history = [obs(115), *finals(120)]  # finals at 120:00, 120:45, 121:30
    d = decide(history, 121.5)
    assert (d.outcome, d.reason) == (Outcome.CONFIRMED, Reason.CONFIRMED)
    assert (d.observation.home_score, d.observation.away_score) == (82, 79)
    assert d.observation.overtime_periods == 0


def test_final_waits_for_enough_observations():
    d = decide(finals(120, count=2, every_s=120), 122)
    assert (d.outcome, d.reason) == (Outcome.WAIT, Reason.CONFIRMING)


def test_final_waits_for_enough_time():
    d = decide(finals(120, count=4, every_s=20), 121)
    assert (d.outcome, d.reason) == (Outcome.WAIT, Reason.CONFIRMING)


def test_clock_at_zero_without_final_status_is_not_final():
    # End of Q4 on the clock is not a final: only an explicit final status counts.
    history = [obs(m, Status.LIVE, current_period=4) for m in (118, 119, 120, 121)]
    d = decide(history, 121)
    assert (d.outcome, d.reason) == (Outcome.WAIT, Reason.IN_PROGRESS)


def test_history_order_does_not_matter():
    history = [obs(115), *finals(120)]
    shuffled = history[:]
    random.Random(4).shuffle(shuffled)
    assert decide(shuffled, 121.5) == decide(history, 121.5)


# -- overtime -------------------------------------------------------------------


def test_overtime_final_is_confirmed_with_ot_count():
    periods = (*TIED_REGULATION, (12, 9))
    d = decide(finals(130, periods=periods), 131.5)
    assert d.outcome is Outcome.CONFIRMED
    assert d.observation.overtime_periods == 1
    assert (d.observation.home_score, d.observation.away_score) == (92, 89)


def test_double_overtime_final():
    periods = (*TIED_REGULATION, (10, 10), (13, 7))
    d = decide(finals(140, periods=periods), 141.5)
    assert d.outcome is Outcome.CONFIRMED
    assert d.observation.overtime_periods == 2


def test_overtime_after_untied_regulation_is_held():
    periods = (*REGULATION, (5, 3))
    d = decide(finals(130, periods=periods), 131.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.INCONSISTENT_RESULT)


# -- final: inconsistent data ---------------------------------------------------


@pytest.mark.parametrize(
    "periods, score",
    [
        (TIED_REGULATION, None),  # tied final
        (REGULATION, (83, 79)),  # periods don't add up
        (REGULATION[:3], None),  # only three periods
        (REGULATION, (-1, 79)),  # negative
    ],
    ids=["tie", "sum-mismatch", "three-periods", "negative"],
)
def test_inconsistent_final_is_held(periods, score):
    d = decide(finals(120, periods=periods, score=score), 121.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.INCONSISTENT_RESULT)


def test_score_change_inside_final_window_is_held():
    history = [*finals(120, count=2), obs(121.5, Status.FINAL, score=(84, 79), periods=((22, 18), *REGULATION[1:]))]
    d = decide(history, 121.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.RESULT_CHANGED)


def test_final_then_live_again_is_held():
    history = [*finals(120, count=2), obs(121.5, Status.LIVE)]
    d = decide(history, 121.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.STATUS_REGRESSED)


# -- postponed / unknown / wrong teams ------------------------------------------


@pytest.mark.parametrize("status", [Status.POSTPONED, Status.SUSPENDED, Status.CANCELLED, Status.ABANDONED])
def test_not_playable_is_held(status):
    d = decide([obs(-30, Status.SCHEDULED), obs(5, status, score=(0, 0), periods=())], 5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.NOT_PLAYABLE)


def test_unknown_status_is_held():
    d = decide([obs(120, Status.UNKNOWN, raw_status="walkover")], 120)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.UNKNOWN_STATUS)
    assert "walkover" in d.detail


def test_team_change_is_held():
    history = [obs(60), *finals(120, teams=("PAN", "OLY"))]
    d = decide(history, 121.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.TEAM_MISMATCH)


def test_unmapped_team_is_held():
    d = decide(finals(120), 121.5, fx=fixture(away_id=None))
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.UNMAPPED_TEAM)


def test_observation_for_another_match_is_held():
    other = MatchKey(Competition.EUROLEAGUE, "2026-27", "test-source", "E2026_43")
    d = decide(finals(120, key=other), 121.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.WRONG_MATCH)


# -- staleness ------------------------------------------------------------------


def test_stale_cached_data_does_not_count_toward_confirmation():
    history = finals(120, count=4, data_lag_s=600)  # CDN serving a 10-minute-old copy
    d = decide(history, 122.25)
    assert (d.outcome, d.reason) == (Outcome.WAIT, Reason.CONFIRMING)


def test_persistently_stale_source_is_held():
    d = decide(finals(120, data_lag_s=1500), 121.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.SOURCE_STALE)


def test_short_gap_after_final_waits_for_fresh_data():
    d = decide(finals(120), 126)  # last observation 4.5 min old
    assert (d.outcome, d.reason) == (Outcome.WAIT, Reason.AWAITING_DATA)


def test_source_outage_during_game_is_held():
    d = decide([obs(60)], 90)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.SOURCE_STALE)


# -- timing ---------------------------------------------------------------------


def test_final_before_tip_off_is_held():
    # A fixture in the future cannot already be final, however consistent the source is.
    d = decide(finals(-30), -28.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.IMPLAUSIBLE_TIMING)


def test_final_implausibly_soon_after_tip_off_is_held():
    d = decide(finals(35), 36.5)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.IMPLAUSIBLE_TIMING)


def test_halftime_implausibly_soon_after_tip_off_is_held():
    history = [obs(10 + i * 0.75, Status.HALFTIME, HALF) for i in range(3)]
    d = decide(history, 11.5, kind=HALFTIME)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.IMPLAUSIBLE_TIMING)


def test_before_tip_off_waits():
    assert decide([], -20).reason is Reason.AWAITING_DATA
    assert decide([obs(-20, Status.SCHEDULED, score=(0, 0), periods=())], -20).reason is Reason.NOT_STARTED


def test_no_final_state_after_max_duration_is_held():
    history = [obs(m) for m in range(200, 245, 1)]
    d = decide(history, 244)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.NO_FINAL_STATE)


def test_no_observations_after_max_duration_is_held():
    d = decide([], 245)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.NO_FINAL_STATE)


# -- halftime -------------------------------------------------------------------


def test_halftime_confirmed_from_explicit_state():
    history = [obs(50, Status.LIVE, HALF, current_period=2)] + [
        obs(55 + i * 0.75, Status.HALFTIME, HALF) for i in range(3)
    ]
    d = decide(history, 56.5, kind=HALFTIME)
    assert d.outcome is Outcome.CONFIRMED
    assert (d.observation.home_score, d.observation.away_score) == (41, 40)


def test_tied_halftime_is_fine():
    history = [obs(55 + i * 0.75, Status.HALFTIME, ((20, 20), (20, 20))) for i in range(3)]
    assert decide(history, 56.5, kind=HALFTIME).outcome is Outcome.CONFIRMED


def test_end_of_second_period_is_not_halftime():
    history = [obs(m, Status.LIVE, HALF, current_period=2) for m in (55, 56, 57)]
    assert decide(history, 57, kind=HALFTIME).reason is Reason.IN_PROGRESS


def test_game_resuming_before_confirmation_passes_the_window():
    history = [obs(55, Status.HALFTIME, HALF), obs(56, Status.LIVE, (*HALF, (2, 0)), current_period=3)]
    d = decide(history, 56, kind=HALFTIME)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.WINDOW_PASSED)


def test_halftime_never_seen_passes_the_window():
    d = decide([obs(70, Status.LIVE, (*HALF, (10, 8)), current_period=3)], 70, kind=HALFTIME)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.WINDOW_PASSED)


def test_halftime_with_wrong_period_count_is_held():
    history = [obs(55 + i * 0.75, Status.HALFTIME, REGULATION[:3]) for i in range(3)]
    d = decide(history, 56.5, kind=HALFTIME)
    assert (d.outcome, d.reason) == (Outcome.HOLD, Reason.INCONSISTENT_RESULT)


def test_key_is_passed_through():
    assert decide(finals(120), 121.5).observation.key == KEY
