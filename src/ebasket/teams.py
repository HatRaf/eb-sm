"""TeamCatalog: canonical team IDs and explicit per-source mappings.

No fuzzy name matching anywhere: a source team ref either appears in the
catalog or the match is held as unmapped.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model import Fixture

TEAM_ID = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class Team(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name_en: str
    # Greek display forms come from Elena (OPEN_DECISIONS E5); captions never generate them.
    names_el: dict[str, str] = Field(default_factory=dict)
    source_refs: dict[str, list[str]] = Field(default_factory=dict)


class TeamCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    teams: dict[str, Team]

    @model_validator(mode="after")
    def _check(self) -> TeamCatalog:
        seen: dict[tuple[str, str], str] = {}
        for team_id, team in self.teams.items():
            if not re.match(TEAM_ID, team_id):
                raise ValueError(f"team id {team_id!r} must be a lowercase slug")
            for source, refs in team.source_refs.items():
                for ref in refs:
                    other = seen.setdefault((source, ref), team_id)
                    if other != team_id:
                        raise ValueError(f"{source} ref {ref!r} mapped to both {other} and {team_id}")
        return self

    @classmethod
    def load(cls, path: str | Path) -> TeamCatalog:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    def resolve(self, source: str, ref: str) -> str | None:
        for team_id, team in self.teams.items():
            if ref in team.source_refs.get(source, ()):
                return team_id
        return None

    def resolve_fixture(self, fixture: Fixture) -> Fixture:
        source = fixture.key.source
        return fixture.with_team_ids(
            self.resolve(source, fixture.home_team_ref), self.resolve(source, fixture.away_team_ref)
        )
