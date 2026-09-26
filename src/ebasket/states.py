"""Event and job state machines plus the job keys that prevent duplicate posts."""

from __future__ import annotations

import enum
from zoneinfo import ZoneInfo

from .model import EventKind, Fixture


class EventState(enum.StrEnum):
    OBSERVED = "observed"
    CONFIRMED = "confirmed"
    HELD = "held"


class JobState(enum.StrEnum):
    CONFIRMED = "confirmed"
    RENDERED = "rendered"
    VALIDATED = "validated"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    SHADOWED = "shadowed"  # shadow mode: NoOp "publish", never a real post
    PUBLISH_UNKNOWN = "publish_unknown"
    HELD = "held"
    DISMISSED = "dismissed"


EVENT_TRANSITIONS: dict[EventState, frozenset[EventState]] = {
    EventState.OBSERVED: frozenset({EventState.CONFIRMED, EventState.HELD}),
    EventState.CONFIRMED: frozenset({EventState.HELD}),
    EventState.HELD: frozenset({EventState.OBSERVED}),  # manual retry only
}

JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.CONFIRMED: frozenset({JobState.RENDERED, JobState.HELD}),
    JobState.RENDERED: frozenset({JobState.VALIDATED, JobState.HELD}),
    JobState.VALIDATED: frozenset({JobState.READY, JobState.HELD}),
    JobState.READY: frozenset({JobState.PUBLISHING, JobState.SHADOWED, JobState.HELD}),
    JobState.PUBLISHING: frozenset({JobState.PUBLISHED, JobState.PUBLISH_UNKNOWN, JobState.HELD}),
    # Never retried blindly: only reconciliation (or a person) moves it on.
    JobState.PUBLISH_UNKNOWN: frozenset({JobState.PUBLISHED, JobState.HELD}),
    # Manual resolution: retry from the start, dismiss, or record a post made by hand.
    JobState.HELD: frozenset({JobState.CONFIRMED, JobState.DISMISSED, JobState.PUBLISHED}),
    JobState.PUBLISHED: frozenset(),
    JobState.SHADOWED: frozenset(),
    JobState.DISMISSED: frozenset(),
}

TERMINAL_JOB_STATES = frozenset(s for s, targets in JOB_TRANSITIONS.items() if not targets)


class IllegalTransition(Exception):
    pass


def check_job_transition(current: JobState, to: JobState) -> None:
    if to not in JOB_TRANSITIONS[current]:
        raise IllegalTransition(f"job cannot go from {current} to {to}")


def check_event_transition(current: EventState, to: EventState) -> None:
    if to not in EVENT_TRANSITIONS[current]:
        raise IllegalTransition(f"event cannot go from {current} to {to}")


def job_key(fixture: Fixture, kind: EventKind, platform: str, fmt: str) -> str:
    """The spec's unique key, with the source added so two providers' match IDs can never collide.

    competition + season + source + source match ID + event + platform + format.
    """
    k = fixture.key
    return f"{k.competition}:{k.season}:{k.source}:{k.source_match_id}:{kind}:{platform}:{fmt}"


def natural_key(fixture: Fixture, kind: EventKind, platform: str, fmt: str) -> str:
    """Second duplicate guard that survives a change of score source (new source IDs).

    Same competition, Athens calendar day, canonical teams, event, destination.
    """
    if fixture.home_team_id is None or fixture.away_team_id is None:
        raise ValueError("natural key needs canonical team IDs")
    day = fixture.start_utc.astimezone(ZoneInfo("Europe/Athens")).date().isoformat()
    k = fixture.key
    return f"{k.competition}:{day}:{fixture.home_team_id}:{fixture.away_team_id}:{kind}:{platform}:{fmt}"
