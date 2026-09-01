"""Pinned model specs.

The arguer is one pinned model for the whole study (a design commitment, not a
convenience). Delegate and scan models are separable so the released benchmark
can be re-scored on anything OpenRouter serves.
"""
from __future__ import annotations

from .openrouter import ModelSpec

PINNED = {
    # role-default -> OpenRouter model id
    "interlocutor": "anthropic/claude-sonnet-4.5",
    "delegate": "anthropic/claude-sonnet-4.5",
    "lean_scan": "anthropic/claude-sonnet-4.5",
}


def spec(role: str, *, seed: int | None = None, **overrides) -> ModelSpec:
    base = dict(model=PINNED[role], temperature=1.0, max_tokens=700, seed=seed)
    base.update(overrides)
    return ModelSpec(**base)
