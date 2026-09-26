from dataclasses import replace
from datetime import UTC, datetime

import pytest
import yaml
from helpers import ROOT, fixture
from pydantic import ValidationError

from ebasket.config import Mode, Settings
from ebasket.model import EventKind
from ebasket.publishers.base import PostDraft, PublishOutcome
from ebasket.publishers.noop import NoOpPublisher
from ebasket.states import job_key, natural_key
from ebasket.teams import TeamCatalog

SETTINGS = ROOT / "config" / "settings.yaml"


def settings_dict():
    return yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))


def test_repo_settings_default_to_preview():
    s = Settings.load(SETTINGS)
    assert s.mode is Mode.PREVIEW
    assert s.detector.to_config().confirm_count == 3


def test_live_mode_is_refused_until_a_publisher_exists():
    with pytest.raises(ValidationError, match="live"):
        Settings.model_validate(settings_dict() | {"mode": "live"})


def test_unknown_settings_key_is_refused():
    with pytest.raises(ValidationError):
        Settings.model_validate(settings_dict() | {"mdoe": "shadow"})


def test_detector_floor_cannot_be_configured_away():
    data = settings_dict()
    data["detector"]["confirm_count"] = 1
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_team_catalog_resolves_only_explicit_refs():
    catalog = TeamCatalog.load(ROOT / "config" / "teams.yaml")
    assert catalog.resolve("euroleague-incrowd", "OLY") == "olympiacos"
    assert catalog.resolve("euroleague-incrowd", "oly") is None
    assert catalog.resolve("euroleague-incrowd", "Olympiacos") is None
    assert catalog.resolve("some-other-source", "OLY") is None


def test_team_catalog_rejects_double_mapping():
    with pytest.raises(ValidationError, match="mapped to both"):
        TeamCatalog.model_validate(
            {
                "version": 1,
                "teams": {
                    "a": {"name_en": "A", "source_refs": {"s": ["X"]}},
                    "b": {"name_en": "B", "source_refs": {"s": ["X"]}},
                },
            }
        )


def test_team_catalog_rejects_bad_ids():
    with pytest.raises(ValidationError):
        TeamCatalog.model_validate({"version": 1, "teams": {"Olympiacos FC": {"name_en": "x"}}})


def test_job_key_matches_spec():
    assert job_key(fixture(), EventKind.FINAL, "instagram", "feed-4x5") == (
        "EUROLEAGUE:2026-27:E2026_42:FINAL:instagram:feed-4x5"
    )


def test_natural_key_uses_athens_calendar_day():
    late = replace(fixture(), start_utc=datetime(2026, 10, 1, 22, 30, tzinfo=UTC))  # 01:30 in Athens
    assert natural_key(late, EventKind.FINAL, "x", "y").startswith("EUROLEAGUE:2026-10-02:olympiacos:panathinaikos")


def test_natural_key_needs_canonical_teams():
    with pytest.raises(ValueError):
        natural_key(fixture(home_id=None), EventKind.FINAL, "x", "y")


def test_noop_publisher_sends_nothing():
    pub = NoOpPublisher()
    draft = PostDraft("k", "instagram", "feed-4x5", "caption", "/output/x.jpg", "ab" * 32)
    result = pub.publish(draft)
    assert result.outcome is PublishOutcome.SHADOW and result.platform_post_id is None
    assert pub.sends_externally is False and pub.drafts == [draft]
