"""Publisher interface. A real platform adapter is added only after Elena picks the destination."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


class PublishOutcome(enum.StrEnum):
    PUBLISHED = "published"  # platform confirmed; post ID returned
    FAILED = "failed"  # platform definitely did not publish
    UNKNOWN = "unknown"  # request may or may not have landed -> reconcile, never blind retry
    SHADOW = "shadow"  # NoOp: nothing was sent anywhere


@dataclass(frozen=True)
class PostDraft:
    job_key: str
    platform: str
    format: str
    caption: str
    image_path: str
    image_sha256: str


@dataclass(frozen=True)
class PublishResult:
    outcome: PublishOutcome
    platform_post_id: str | None = None
    http_status: int | None = None
    detail: str = ""


class Publisher(Protocol):
    name: str
    sends_externally: bool

    def publish(self, draft: PostDraft) -> PublishResult: ...

    def reconcile(self, draft: PostDraft) -> PublishResult: ...
