"""Normalized match data shared by every source adapter.

Source IDs and team refs are opaque strings. Canonical team IDs come from the
TeamCatalog, never from fuzzy matching of names. All datetimes are aware UTC.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

REGULATION_PERIODS = 4


class Competition(enum.StrEnum):
    EUROLEAGUE = "EUROLEAGUE"
    GBL = "GBL"


class Status(enum.StrEnum):
    SCHEDULED = "SCHEDULED"
    LIVE = "LIVE"
    HALFTIME = "HALFTIME"
    FINAL = "FINAL"
    POSTPONED = "POSTPONED"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"
    UNKNOWN = "UNKNOWN"


NOT_PLAYABLE = frozenset({Status.POSTPONED, Status.SUSPENDED, Status.CANCELLED, Status.ABANDONED})
IN_PROGRESS = frozenset({Status.LIVE, Status.HALFTIME})


class EventKind(enum.StrEnum):
    HALFTIME = "HALFTIME"
    FINAL = "FINAL"


def require_utc(name: str, value: datetime | None) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC datetime, got {value!r}")


@dataclass(frozen=True)
class MatchKey:
    competition: Competition
    season: str
    source: str
    source_match_id: str

    def __str__(self) -> str:
        return f"{self.competition}:{self.season}:{self.source_match_id}"


@dataclass(frozen=True)
class Fixture:
    """A scheduled game as first learned from the source's fixture list."""

    key: MatchKey
    start_utc: datetime
    home_team_ref: str
    away_team_ref: str
    # Filled in by TeamCatalog.resolve_fixture(); None means "unmapped" -> hold.
    home_team_id: str | None = None
    away_team_id: str | None = None

    def __post_init__(self) -> None:
        require_utc("start_utc", self.start_utc)

    def with_team_ids(self, home: str | None, away: str | None) -> Fixture:
        return replace(self, home_team_id=home, away_team_id=away)


@dataclass(frozen=True)
class Observation:
    """One normalized reading of a game from a source.

    `source_asof_utc` is the time the source asserts this data was current
    (e.g. response generation time). It detects stale cached copies; None if
    the source gives nothing usable, in which case fetch time is used.
    """

    key: MatchKey
    start_utc: datetime
    home_team_ref: str
    away_team_ref: str
    status: Status
    raw_status: str
    home_score: int | None
    away_score: int | None
    # (home, away) per played period: regulation first, then each overtime.
    period_scores: tuple[tuple[int, int], ...]
    current_period: int | None
    source_asof_utc: datetime | None
    fetched_utc: datetime
    raw_sha256: str

    def __post_init__(self) -> None:
        require_utc("start_utc", self.start_utc)
        require_utc("source_asof_utc", self.source_asof_utc)
        require_utc("fetched_utc", self.fetched_utc)

    @property
    def overtime_periods(self) -> int:
        return max(0, len(self.period_scores) - REGULATION_PERIODS)

    @property
    def data_age(self) -> timedelta:
        """How old the data was when we fetched it (clock skew clamps to zero)."""
        if self.source_asof_utc is None:
            return timedelta(0)
        return max(timedelta(0), self.fetched_utc - self.source_asof_utc)

    def result(self) -> tuple:
        """Everything that must stay identical across a confirmation window."""
        return (
            self.home_team_ref,
            self.away_team_ref,
            self.home_score,
            self.away_score,
            self.period_scores,
        )

    def fixture(self) -> Fixture:
        return Fixture(self.key, self.start_utc, self.home_team_ref, self.away_team_ref)
