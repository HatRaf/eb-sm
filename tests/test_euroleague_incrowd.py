"""The EuroLeague adapter against real recorded responses (docs/SCORE_SOURCES.md)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from helpers import FIXTURES, ROOT

from ebasket.detector import Outcome, detect
from ebasket.model import EventKind, Status
from ebasket.sources import euroleague_incrowd as src
from ebasket.sources.base import SourceFormatError
from ebasket.teams import TeamCatalog

DIR = FIXTURES / "euroleague_incrowd"
FETCHED = datetime(2026, 9, 26, 16, 30, tzinfo=UTC)


def load(name: str) -> bytes:
    return (DIR / name).read_bytes()


def game_obj(name: str) -> dict:
    return json.loads(load(name))["data"]


def test_round_one_finals():
    games = src.parse_games(load("games_E2026_round1_finals.json"), FETCHED)
    assert len(games) == 10
    for g in games:
        assert g.status is Status.FINAL and g.raw_status == "result"
        assert g.key.season == "2026-27" and g.key.source_match_id.startswith("E2026_")
        assert len(g.period_scores) == 4
        assert sum(h for h, _ in g.period_scores) == g.home_score
        assert sum(a for _, a in g.period_scores) == g.away_score
    pan = next(g for g in games if g.key.source_match_id == "E2026_7")
    assert (pan.home_team_ref, pan.away_team_ref, pan.home_score, pan.away_score) == ("PAN", "PRS", 91, 72)
    assert [h for h, _ in pan.period_scores] == [31, 11, 27, 22]
    assert pan.start_utc == datetime(2026, 9, 24, 18, 15, tzinfo=UTC)


def test_double_overtime_game():
    g = src.parse_game_response(load("game_E2025_340_final_2ot.json"), FETCHED)
    assert (g.key.season, g.key.source_match_id) == ("2025-26", "E2025_340")
    assert (g.home_score, g.away_score) == (110, 104)
    assert g.overtime_periods == 2
    assert g.period_scores[4:] == ((13, 13), (13, 7))


def test_triple_overtime_game():
    games = src.parse_games(load("games_E2025_sample.json"), FETCHED)
    g = next(g for g in games if g.key.source_match_id == "E2025_168")
    assert g.overtime_periods == 3


def test_scheduled_game_has_no_scores():
    # The feed reports 0 and zero quarters for unplayed games; those must not look like a score.
    g = src.parse_game_response(load("game_E2026_379_scheduled.json"), FETCHED)
    assert g.status is Status.SCHEDULED and g.raw_status == "confirmed"
    assert (g.home_score, g.away_score, g.period_scores) == (None, None, ())


def test_source_asof_parses_nanosecond_timestamp():
    g = src.parse_game_response(load("game_E2026_379_scheduled.json"), FETCHED)
    assert g.source_asof_utc is not None and g.source_asof_utc.tzinfo is not None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postponed", Status.POSTPONED),
        ("cancelled", Status.CANCELLED),
        ("suspended", Status.SUSPENDED),
        ("walkover", Status.UNKNOWN),
        ("something-new", Status.UNKNOWN),
    ],
)
def test_status_mapping(raw, expected):
    game = game_obj("game_E2026_379_scheduled.json") | {"status": raw}
    assert src.parse_game(game, fetched_utc=FETCHED, source_asof_utc=None).status is expected


def test_malformed_responses_raise():
    with pytest.raises(SourceFormatError):
        src.parse_game_response(b"", FETCHED)  # live.euroleague.net does this pre-game
    with pytest.raises(SourceFormatError):
        src.parse_game_response(b"<html>", FETCHED)
    game = game_obj("game_E2025_340_final_2ot.json")
    del game["home"]
    with pytest.raises(SourceFormatError):
        src.parse_game(game, fetched_utc=FETCHED, source_asof_utc=None)


def test_one_sided_period_raises():
    game = game_obj("game_E2025_340_final_2ot.json")
    game["away"]["quarters"]["ot2"] = None
    with pytest.raises(SourceFormatError):
        src.parse_game(game, fetched_utc=FETCHED, source_asof_utc=None)


def test_raw_hash_tracks_content():
    game = game_obj("game_E2025_340_final_2ot.json")
    a = src.parse_game(game, fetched_utc=FETCHED, source_asof_utc=None)
    b = src.parse_game(dict(game), fetched_utc=FETCHED + timedelta(minutes=1), source_asof_utc=None)
    c = src.parse_game(game | {"minute": "49:59"}, fetched_utc=FETCHED, source_asof_utc=None)
    assert a.raw_sha256 == b.raw_sha256 != c.raw_sha256


def test_urls():
    assert src.games_url("2026-27").endswith("/seasons/E2026/games?limit=400")
    assert src.game_url("2026-27", "E2026_7").endswith("/seasons/E2026/games/7")


def test_catalog_covers_every_2026_27_team():
    catalog = TeamCatalog.load(ROOT / "config" / "teams.yaml")
    for g in src.parse_games(load("games_E2026_round1_finals.json"), FETCHED):
        fx = catalog.resolve_fixture(g.fixture())
        assert fx.home_team_id and fx.away_team_id, g.key


def test_recorded_round_confirms_end_to_end():
    """Replay each real final three times, 45 s apart, through catalog + detector."""
    catalog = TeamCatalog.load(ROOT / "config" / "teams.yaml")
    body = load("games_E2026_round1_finals.json")
    first_fetch = src.parse_games(body, FETCHED)[0].source_asof_utc + timedelta(seconds=1)
    polls = [src.parse_games(body, first_fetch + timedelta(seconds=45 * i)) for i in range(3)]
    for history in zip(*polls):
        fx = catalog.resolve_fixture(history[0].fixture())
        d = detect(EventKind.FINAL, fx, history, history[-1].fetched_utc)
        assert d.outcome is Outcome.CONFIRMED, (fx.key, d)
