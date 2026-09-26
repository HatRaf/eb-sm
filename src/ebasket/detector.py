"""EventDetector: observation history -> wait / confirmed / hold. Pure, no I/O.

HALFTIME and FINAL are only ever confirmed from an explicit source status seen
consistently across a confirmation window — never inferred from the clock or
from the end of a period. Anything uncertain is a hold with a reason.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import (
    NOT_PLAYABLE,
    REGULATION_PERIODS,
    EventKind,
    Fixture,
    Observation,
    Status,
)


class Outcome(enum.StrEnum):
    WAIT = "wait"
    CONFIRMED = "confirmed"
    HOLD = "hold"


class Reason(enum.StrEnum):
    # wait
    AWAITING_DATA = "awaiting_data"
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    CONFIRMING = "confirming"
    # hold
    UNMAPPED_TEAM = "unmapped_team"
    WRONG_MATCH = "wrong_match"
    TEAM_MISMATCH = "team_mismatch"
    NOT_PLAYABLE = "not_playable"
    UNKNOWN_STATUS = "unknown_status"
    STATUS_REGRESSED = "status_regressed"
    RESULT_CHANGED = "result_changed"
    INCONSISTENT_RESULT = "inconsistent_result"
    SOURCE_STALE = "source_stale"
    NO_FINAL_STATE = "no_final_state"
    WINDOW_PASSED = "window_passed"
    # confirmed
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class DetectorConfig:
    confirm_count: int = 3
    confirm_span: timedelta = timedelta(seconds=90)
    stale_after: timedelta = timedelta(minutes=3)
    outage_hold_after: timedelta = timedelta(minutes=20)
    max_game_duration: timedelta = timedelta(hours=4)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    kind: EventKind
    reason: Reason
    detail: str = ""
    observation: Observation | None = None  # the confirmed snapshot


_TARGET = {EventKind.FINAL: Status.FINAL, EventKind.HALFTIME: Status.HALFTIME}


def detect(
    kind: EventKind,
    fixture: Fixture,
    history: Sequence[Observation],
    now: datetime,
    cfg: DetectorConfig = DetectorConfig(),
) -> Decision:
    def wait(reason: Reason, detail: str = "") -> Decision:
        return Decision(Outcome.WAIT, kind, reason, detail)

    def hold(reason: Reason, detail: str) -> Decision:
        return Decision(Outcome.HOLD, kind, reason, detail)

    if fixture.home_team_id is None or fixture.away_team_id is None:
        return hold(
            Reason.UNMAPPED_TEAM,
            f"source teams {fixture.home_team_ref!r} / {fixture.away_team_ref!r} not in team catalog",
        )

    obs = sorted(history, key=lambda o: o.fetched_utc)
    for o in obs:
        if o.key != fixture.key:
            return hold(Reason.WRONG_MATCH, f"observation for {o.key} given to {fixture.key}")
        if (o.home_team_ref, o.away_team_ref) != (fixture.home_team_ref, fixture.away_team_ref):
            return hold(
                Reason.TEAM_MISMATCH,
                f"fixture {fixture.home_team_ref}-{fixture.away_team_ref}, "
                f"source reported {o.home_team_ref}-{o.away_team_ref}",
            )

    overdue = now - fixture.start_utc > cfg.max_game_duration
    too_late = Reason.NO_FINAL_STATE if kind is EventKind.FINAL else Reason.WINDOW_PASSED

    if not obs:
        if overdue:
            return hold(too_late, "no observations at all")
        return wait(Reason.AWAITING_DATA)

    last = obs[-1]
    if last.status in NOT_PLAYABLE:
        return hold(Reason.NOT_PLAYABLE, f"source status {last.raw_status!r}")
    if last.status is Status.UNKNOWN:
        return hold(Reason.UNKNOWN_STATUS, f"unrecognized source status {last.raw_status!r}")

    silence = now - last.fetched_utc
    if now >= fixture.start_utc and silence > cfg.outage_hold_after:
        return hold(Reason.SOURCE_STALE, f"no observation for {_fmt(silence)}")
    if last.data_age > cfg.outage_hold_after:
        return hold(Reason.SOURCE_STALE, f"source is serving data {_fmt(last.data_age)} old")

    target = _TARGET[kind]
    first = next((i for i, o in enumerate(obs) if o.status is target), None)
    if first is None:
        if kind is EventKind.HALFTIME and _past_halftime(last):
            return hold(Reason.WINDOW_PASSED, f"no halftime state seen; source now {last.raw_status!r}")
        if overdue:
            return hold(too_late, f"{_fmt(now - fixture.start_utc)} after tip-off, source still {last.raw_status!r}")
        if last.status is Status.SCHEDULED:
            return wait(Reason.NOT_STARTED)
        return wait(Reason.IN_PROGRESS, f"source {last.raw_status!r}")

    window = obs[first:]
    moved_on = next((o for o in window if o.status is not target), None)
    if moved_on is not None:
        if kind is EventKind.HALFTIME:
            return hold(Reason.WINDOW_PASSED, "game resumed before halftime was confirmed")
        return hold(Reason.STATUS_REGRESSED, f"source went from final back to {moved_on.raw_status!r}")
    if len({o.result() for o in window}) > 1:
        return hold(Reason.RESULT_CHANGED, f"score changed after the source reported {target}")
    problem = _inconsistency(kind, last)
    if problem:
        return hold(Reason.INCONSISTENT_RESULT, problem)

    if silence > cfg.stale_after:
        return wait(Reason.AWAITING_DATA, f"latest observation is {_fmt(silence)} old")

    # Only observations whose data was fresh when fetched count toward confirmation.
    run: list[Observation] = []
    for o in reversed(window):
        if o.data_age > cfg.stale_after:
            break
        run.append(o)
    run.reverse()
    span = run[-1].fetched_utc - run[0].fetched_utc if run else timedelta(0)
    if len(run) >= cfg.confirm_count and span >= cfg.confirm_span:
        return Decision(
            Outcome.CONFIRMED, kind, Reason.CONFIRMED, f"{len(run)} observations over {_fmt(span)}", last
        )
    return wait(Reason.CONFIRMING, f"{len(run)}/{cfg.confirm_count} fresh observations over {_fmt(span)}")


def _past_halftime(o: Observation) -> bool:
    return o.status is Status.FINAL or (o.current_period is not None and o.current_period > 2)


def _inconsistency(kind: EventKind, o: Observation) -> str | None:
    if o.home_score is None or o.away_score is None:
        return "score missing"
    periods = o.period_scores
    if o.home_score < 0 or o.away_score < 0 or any(h < 0 or a < 0 for h, a in periods):
        return "negative score"
    if kind is EventKind.FINAL:
        if len(periods) < REGULATION_PERIODS:
            return f"final reported with only {len(periods)} periods"
        if o.home_score == o.away_score:
            return f"final reported as a tie {o.home_score}-{o.away_score}"
        # Overtime is only played from a level score: regulation and every OT but the last must end tied.
        home = away = 0
        for i, (h, a) in enumerate(periods[:-1], start=1):
            home, away = home + h, away + a
            if i >= REGULATION_PERIODS and home != away:
                return f"overtime period {len(periods) - REGULATION_PERIODS} played after a non-tied period {i}"
    elif len(periods) != 2:
        return f"halftime reported with {len(periods)} periods"
    if (sum(h for h, _ in periods), sum(a for _, a in periods)) != (o.home_score, o.away_score):
        return f"period scores do not add up to {o.home_score}-{o.away_score}"
    return None


def _fmt(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"
