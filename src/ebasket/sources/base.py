"""ScoreSource interface. Adapters map only explicit source states."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..model import Fixture, Observation


class SourceFormatError(Exception):
    """The source answered, but not in the shape the adapter was built against."""


@dataclass(frozen=True)
class SourceCapabilities:
    explicit_final: bool
    explicit_halftime: bool
    per_overtime_scores: bool  # False if all OTs are merged into one number
    min_poll_interval_s: int


class ScoreSource(Protocol):
    name: str
    capabilities: SourceCapabilities

    def fixtures(self, season: str) -> list[Fixture]: ...

    def observe(self, fixture: Fixture) -> Observation: ...
