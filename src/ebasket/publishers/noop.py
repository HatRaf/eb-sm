"""NoOpPublisher: records what would have been posted; sends nothing."""

from __future__ import annotations

from .base import PostDraft, PublishOutcome, PublishResult


class NoOpPublisher:
    name = "noop"
    sends_externally = False

    def __init__(self) -> None:
        self.drafts: list[PostDraft] = []

    def publish(self, draft: PostDraft) -> PublishResult:
        self.drafts.append(draft)
        return PublishResult(
            PublishOutcome.SHADOW,
            detail=f"would publish {draft.image_sha256[:12]} to {draft.platform}/{draft.format}",
        )

    def reconcile(self, draft: PostDraft) -> PublishResult:
        return PublishResult(PublishOutcome.SHADOW, detail="noop: nothing to reconcile")
