"""The arguer: an open-ended debate partner that argues one fixed side.

Named `interlocutor` throughout so that "arguer", "delegate" and "principal"
stay distinct in the codebase and in the released dataset.
"""
from __future__ import annotations

import random
from typing import Any

from ..config import CONSTRAINED_EXCHANGES, CONSTRAINT_CELLS, CONSTRAINT_POOL
from .base import Agent, AgentConfig, Scene

STANCE_TEXT = {
    "for": "in favour of the proposition",
    "against": "against the proposition",
}


class Interlocutor(Agent):
    self_speaker = "interlocutor"
    own_speakers = ("interlocutor",)

    def system_prompt(self, scene: Scene, **kw: Any) -> str:
        stance = kw.get("stance", "against")
        cell = kw.get("constraint_cell") or "free"
        directive = CONSTRAINT_CELLS[cell]["directive"]
        from .base import load_prompt

        return load_prompt("interlocutor").format(
            proposition=scene.proposition,
            background=scene.background or "(none supplied)",
            stance_description=STANCE_TEXT[stance],
            turn_directive=(
                f"CONSTRAINT FOR THIS TURN ONLY\n{directive}" if directive else ""
            ),
        )


def opposing_stance(opening_position: float, midpoint: float = 5.0) -> str:
    """Argue the side the participant is not on. Exact midpoint breaks at random
    so a neutral opener does not systematically get one side."""
    if opening_position > midpoint:
        return "against"
    if opening_position < midpoint:
        return "for"
    return random.choice(["for", "against"])


def build_plan(seed: int, n_exchanges: int) -> dict[str, Any]:
    """Assign constraint cells to exchanges.

    Cells are shuffled per participant so cell is not confounded with position
    in the debate; the set of constrained exchanges is fixed so every
    participant meets the same number of each kind of pressure.
    """
    rng = random.Random(seed)
    cells = list(CONSTRAINT_POOL)
    rng.shuffle(cells)
    schedule = {str(i): "free" for i in range(n_exchanges)}
    for exchange, cell in zip(CONSTRAINED_EXCHANGES, cells):
        if exchange < n_exchanges:
            schedule[str(exchange)] = cell
    return {"n_exchanges": n_exchanges, "cells": schedule, "seed": seed}


def cell_for(plan: dict[str, Any], exchange_idx: int) -> str:
    return (plan.get("cells") or {}).get(str(exchange_idx), "free")


def interlocutor_config(model: str, seed: int | None = None) -> AgentConfig:
    from ..llm.openrouter import ModelSpec

    return AgentConfig(
        name=f"interlocutor::{model}",
        role="interlocutor",
        spec=ModelSpec(model=model, temperature=1.0, max_tokens=400, seed=seed),
        prompt_variant="standard",
        context_policy="full",
        profile_kind="none",
        rating_mode="none",
    )
