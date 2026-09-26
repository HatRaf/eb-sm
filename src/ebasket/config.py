"""Settings (non-secret). Secrets never live here — only in env / Docker secrets."""

from __future__ import annotations

import enum
from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .detector import DetectorConfig
from .model import Competition


class Mode(enum.StrEnum):
    PREVIEW = "preview"
    SHADOW = "shadow"
    LIVE = "live"


class CompetitionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    season: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    final_posts: bool = True
    halftime_posts: bool = False  # separate switch (spec); also needs a source with explicit halftime


class DetectorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_count: int = Field(3, ge=2)
    confirm_span_s: int = Field(90, ge=30)
    stale_after_s: int = Field(180, ge=30)
    outage_hold_after_s: int = Field(1200, ge=60)
    max_game_duration_h: float = Field(4.0, ge=2.5)
    min_final_after_tip_min: int = Field(70, ge=45)
    min_halftime_after_tip_min: int = Field(20, ge=15)

    def to_config(self) -> DetectorConfig:
        return DetectorConfig(
            confirm_count=self.confirm_count,
            confirm_span=timedelta(seconds=self.confirm_span_s),
            stale_after=timedelta(seconds=self.stale_after_s),
            outage_hold_after=timedelta(seconds=self.outage_hold_after_s),
            max_game_duration=timedelta(hours=self.max_game_duration_h),
            min_final_after_tip=timedelta(minutes=self.min_final_after_tip_min),
            min_halftime_after_tip=timedelta(minutes=self.min_halftime_after_tip_min),
        )


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Mode = Mode.PREVIEW
    display_timezone: str = "Europe/Athens"
    competitions: dict[Competition, CompetitionSettings]
    detector: DetectorSettings = DetectorSettings()

    @model_validator(mode="after")
    def _no_live_yet(self) -> Settings:
        # There is no real publisher yet (OPEN_DECISIONS E1). The live gate replaces this in M6.
        if self.mode is Mode.LIVE:
            raise ValueError("mode 'live' is not available: no real publisher has been approved")
        return self

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))
