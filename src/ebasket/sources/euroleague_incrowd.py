"""EuroLeague via the InCrowd feed that euroleaguebasketball.net itself uses.

Evidence in docs/SCORE_SOURCES.md. Only `confirmed` and `result` have been
seen in real responses so far; the other status strings come from the site's
JS enum and must be re-checked against recorded live games. This source has
no halftime state, so halftime posts are unsupported for it.

This module only parses. HTTP polling is added with the worker (M4).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from ..model import Competition, MatchKey, Observation, Status
from .base import SourceCapabilities, SourceFormatError

NAME = "euroleague-incrowd"
BASE_URL = "https://feeds.incrowdsports.com/provider/euroleague-feeds/v2/competitions/E"

CAPABILITIES = SourceCapabilities(
    explicit_final=True,
    explicit_halftime=False,
    per_overtime_scores=True,
    min_poll_interval_s=30,
)

_STATUS = {
    "result": Status.FINAL,
    "live": Status.LIVE,
    "confirmed": Status.SCHEDULED,
    "scheduled": Status.SCHEDULED,
    "fixture": Status.SCHEDULED,
    "planned": Status.SCHEDULED,
    "postponed": Status.POSTPONED,
    "suspended": Status.SUSPENDED,
    "cancelled": Status.CANCELLED,
    # "walkover" is deliberately unmapped -> UNKNOWN -> hold: a forfeit is not a normal result post.
}

_PERIODS = ("q1", "q2", "q3", "q4", "ot1", "ot2", "ot3", "ot4", "ot5")


def season_code(season: str) -> str:
    """'2026-27' -> 'E2026'."""
    return f"E{season[:4]}"


def season_label(code: str) -> str:
    """'E2026' -> '2026-27'."""
    m = re.fullmatch(r"E(\d{4})", code)
    if not m:
        raise SourceFormatError(f"unexpected season code {code!r}")
    year = int(m.group(1))
    return f"{year}-{(year + 1) % 100:02d}"


def games_url(season: str) -> str:
    return f"{BASE_URL}/seasons/{season_code(season)}/games?limit=400"


def game_url(season: str, source_match_id: str) -> str:
    code = source_match_id.rsplit("_", 1)[-1]
    return f"{BASE_URL}/seasons/{season_code(season)}/games/{code}"


def parse_games(body: bytes, fetched_utc: datetime) -> list[Observation]:
    envelope = _envelope(body)
    games = envelope.get("data")
    if not isinstance(games, list):
        raise SourceFormatError("games response has no data list")
    asof = _asof(envelope)
    return [parse_game(g, fetched_utc=fetched_utc, source_asof_utc=asof) for g in games]


def parse_game_response(body: bytes, fetched_utc: datetime) -> Observation:
    envelope = _envelope(body)
    game = envelope.get("data")
    if not isinstance(game, dict):
        raise SourceFormatError("game response has no data object")
    return parse_game(game, fetched_utc=fetched_utc, source_asof_utc=_asof(envelope))


def parse_game(game: dict[str, Any], *, fetched_utc: datetime, source_asof_utc: datetime | None) -> Observation:
    try:
        identifier = str(game["identifier"])
        season = season_label(game["season"]["code"])
        raw_status = str(game["status"])
        home, away = game["home"], game["away"]
        home_ref, away_ref = str(home["code"]), str(away["code"])
        start = _utc(game["date"])
    except (KeyError, TypeError) as exc:
        raise SourceFormatError(f"game object missing {exc}") from exc

    status = _STATUS.get(raw_status, Status.UNKNOWN)
    # Scheduled games carry score 0 and zero quarters: scores only count once play has started.
    if status in (Status.LIVE, Status.FINAL):
        home_score, away_score = _int(home.get("score")), _int(away.get("score"))
        periods = _periods(home.get("quarters") or {}, away.get("quarters") or {})
    else:
        home_score = away_score = None
        periods = ()

    canonical = json.dumps(game, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return Observation(
        key=MatchKey(Competition.EUROLEAGUE, season, NAME, identifier),
        start_utc=start,
        home_team_ref=home_ref,
        away_team_ref=away_ref,
        status=status,
        raw_status=raw_status,
        home_score=home_score,
        away_score=away_score,
        period_scores=periods,
        current_period=_period_number(game.get("quarter")),
        source_asof_utc=source_asof_utc,
        fetched_utc=fetched_utc,
        raw_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _envelope(body: bytes) -> dict[str, Any]:
    if not body:
        raise SourceFormatError("empty response body")
    try:
        envelope = json.loads(body)
    except ValueError as exc:
        raise SourceFormatError(f"not JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise SourceFormatError("response is not a JSON object")
    return envelope


def _asof(envelope: dict[str, Any]) -> datetime | None:
    created = (envelope.get("metadata") or {}).get("createdAt")
    return _utc(created) if created else None


def _utc(value: str) -> datetime:
    # e.g. "2026-09-24T18:15:00.000Z" or "2026-09-26T16:17:59.086994972Z" (nanoseconds)
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})", value)
    if not m:
        raise SourceFormatError(f"unexpected timestamp {value!r}")
    fraction = f".{m.group(2)[:6]}" if m.group(2) else ""
    offset = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}{fraction}{offset}").astimezone(UTC)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceFormatError(f"expected an integer score, got {value!r}")
    return value


def _periods(home: dict[str, Any], away: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    periods = []
    missing = None
    for name in _PERIODS:
        h, a = home.get(name), away.get(name)
        if h is None and a is None:
            missing = missing or name
            continue
        if missing:
            raise SourceFormatError(f"period {name} populated after missing {missing}")
        if h is None or a is None:
            raise SourceFormatError(f"period {name} present for only one team")
        periods.append((_int(h), _int(a)))
    return tuple(periods)


def _period_number(quarter: Any) -> int | None:
    # Unverified live format; the site's fixtures show "2" and "OT2".
    if quarter is None:
        return None
    text = str(quarter).strip().upper()
    if text.isdigit():
        return int(text)
    m = re.fullmatch(r"OT(\d+)", text)
    return 4 + int(m.group(1)) if m else None
