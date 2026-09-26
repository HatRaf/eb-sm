"""Builders for synthetic game timelines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ebasket.model import Competition, Fixture, MatchKey, Observation, Status

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

TIP = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
KEY = MatchKey(Competition.EUROLEAGUE, "2026-27", "test-source", "E2026_42")

REGULATION = ((20, 18), (21, 22), (19, 20), (22, 19))  # 82-79
TIED_REGULATION = ((20, 18), (21, 22), (19, 20), (20, 20))  # 80-80
HALF = ((20, 18), (21, 22))  # 41-40


def at(minutes: float) -> datetime:
    return TIP + timedelta(minutes=minutes)


def fixture(home_id: str | None = "olympiacos", away_id: str | None = "panathinaikos") -> Fixture:
    return Fixture(KEY, TIP, "OLY", "PAN", home_id, away_id)


def obs(
    minute: float,
    status: Status = Status.LIVE,
    periods: tuple[tuple[int, int], ...] = REGULATION,
    *,
    raw_status: str | None = None,
    score: tuple[int, int] | None = None,
    teams: tuple[str, str] = ("OLY", "PAN"),
    data_lag_s: float = 2,
    current_period: int | None = None,
    key: MatchKey = KEY,
) -> Observation:
    fetched = at(minute)
    if score is None:
        score = (sum(h for h, _ in periods), sum(a for _, a in periods))
    return Observation(
        key=key,
        start_utc=TIP,
        home_team_ref=teams[0],
        away_team_ref=teams[1],
        status=status,
        raw_status=raw_status or status.lower(),
        home_score=score[0],
        away_score=score[1],
        period_scores=periods,
        current_period=current_period,
        source_asof_utc=fetched - timedelta(seconds=data_lag_s),
        fetched_utc=fetched,
        raw_sha256="0" * 64,
    )


def finals(start_minute: float, count: int = 3, every_s: float = 45, **kwargs) -> list[Observation]:
    return [obs(start_minute + i * every_s / 60, Status.FINAL, **kwargs) for i in range(count)]
