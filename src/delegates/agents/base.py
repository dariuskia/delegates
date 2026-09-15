"""Agent classes.

Two roles - Interlocutor (the arguer) and Delegate - over a shared base. The
things the study varies are not subclasses but fields on an AgentConfig row:
which model, which prompt variant, which profile (or none), and which slice of
the conversation the agent is allowed to see.

Context policy is the load-bearing one:

    full           the delegate sees the interlocutor's turns AND the
                   principal's actual prior replies (full replay)
    opponent_only  the delegate sees the interlocutor's turns but its OWN
                   prior replies in place of the principal's
    none           the delegate sees only the current interlocutor turn

Comparing `full` against `opponent_only` is what separates "reading the
person's recent words" from "modelling the person from their profile".
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from ..llm.openrouter import Completion, ModelSpec, OpenRouter

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
RATING_PREFILL = '{"position":'

Speaker = Literal["interlocutor", "principal", "delegate"]
ContextPolicy = Literal["full", "opponent_only", "none"]


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


@dataclass
class Turn:
    """One utterance. Mirrors a row in `turns`; `id` is None until persisted."""

    idx: int
    speaker: Speaker
    content: str
    id: int | None = None
    act_type: str | None = None
    constraint_cell: str | None = None
    source_turn_id: int | None = None


@dataclass
class Scene:
    """Everything an agent needs to produce its next move."""

    proposition: str
    background: str
    history: Sequence[Turn]
    low_label: str = "Strongly oppose"
    high_label: str = "Strongly support"


@dataclass
class AgentConfig:
    """The persisted definition of an agent class. `id` matches agent_configs."""

    name: str
    role: Literal["interlocutor", "delegate"]
    spec: ModelSpec
    prompt_variant: str = "neutral"
    context_policy: ContextPolicy = "full"
    profile_kind: str = "full"
    rating_mode: Literal["separate_call", "none"] = "separate_call"
    id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Agent:
    """Base: build a system prompt, render the visible history, speak."""

    self_speaker: Speaker = "delegate"
    # Speakers rendered on the assistant side. A delegate standing in for a
    # principal must read the principal's earlier replies as its own turns, or
    # in full replay it would take the person's words for more opponent text.
    own_speakers: tuple[Speaker, ...] = ("delegate",)

    def __init__(self, cfg: AgentConfig, client: OpenRouter | None = None):
        self.cfg = cfg
        self.client = client or OpenRouter()

    # ------------------------------------------------------------- context

    def visible(self, scene: Scene) -> list[Turn]:
        """Apply the context policy to the history."""
        policy = self.cfg.context_policy
        if policy == "full":
            return list(scene.history)
        if policy == "none":
            tail = [t for t in scene.history if t.speaker == "interlocutor"]
            return tail[-1:]
        if policy == "opponent_only":
            # Caller substitutes this agent's own prior replies before calling;
            # anything still attributed to the principal is withheld.
            return [t for t in scene.history if t.speaker != "principal"]
        raise ValueError(f"unknown context policy: {policy}")

    def render_history(self, scene: Scene) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for turn in self.visible(scene):
            role = "assistant" if turn.speaker in self.own_speakers else "user"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n\n" + turn.content
            else:
                messages.append({"role": role, "content": turn.content})
        return messages

    def system_prompt(self, scene: Scene, **kw: Any) -> str:  # pragma: no cover
        raise NotImplementedError

    # --------------------------------------------------------------- speak

    async def speak(
        self,
        scene: Scene,
        *,
        conn: sqlite3.Connection | None = None,
        debate_id: int | None = None,
        **kw: Any,
    ) -> Completion:
        messages = [{"role": "system", "content": self.system_prompt(scene, **kw)}]
        history = self.render_history(scene)
        if not history or history[0]["role"] == "assistant":
            history.insert(0, {"role": "user", "content": "Please begin."})
        messages += history
        return await self.client.complete(
            self.cfg.spec,
            messages,
            conn=conn,
            purpose="turn",
            debate_id=debate_id,
            agent_config_id=self.cfg.id,
        )

    # -------------------------------------------------------------- rating
    # Elicited in a SEPARATE call that never sees prior ratings. Asking for the
    # number in the same breath as the argument invites anchoring on whatever
    # numbers are already in context - which, in full replay, would be the
    # principal's own ratings. That would manufacture the context-condition
    # contrast rather than measure it.

    async def rate(
        self,
        scene: Scene,
        own_reply: str,
        *,
        conn: sqlite3.Connection | None = None,
        debate_id: int | None = None,
        window: int = 4,
    ) -> tuple[float | None, int | None, str]:
        if self.cfg.rating_mode == "none":
            return None, None, ""
        tail = list(scene.history)[-window:]
        lines = []
        for turn in tail:
            who = "OTHER SIDE" if turn.speaker == "interlocutor" else "ME"
            lines.append(f"{who}: {turn.content}")
        lines.append(f"ME: {own_reply}")
        prompt = load_prompt("rating_probe").format(
            proposition=scene.proposition,
            excerpt="\n\n".join(lines),
            low_label=scene.low_label,
            high_label=scene.high_label,
        )
        spec = ModelSpec(
            model=self.cfg.spec.model, temperature=0.0, max_tokens=200,
            seed=self.cfg.spec.seed,
        )
        # Prefill the opening of the JSON object. Without it the model tends
        # to reason aloud first and run out of tokens before the number.
        out = await self.client.complete(
            spec,
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": RATING_PREFILL},
            ],
            conn=conn,
            purpose="rating",
            debate_id=debate_id,
            agent_config_id=self.cfg.id,
        )
        text = out.content
        if not text.lstrip().startswith("{"):
            text = RATING_PREFILL + text
        pos, conf = parse_rating(text)
        return pos, conf, text


def parse_rating(text: str) -> tuple[float | None, int | None]:
    """JSON object first; otherwise only an explicit `position: N`. A bare
    number elsewhere in the text is not accepted - a list item like
    "1. They started..." must not become a rating of 1."""
    match = re.search(r"\{.*?\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            pos = float(data["position"])
            conf = int(data["confidence"]) if data.get("confidence") is not None else None
            return max(0.0, min(10.0, pos)), conf
        except (ValueError, KeyError, TypeError):
            pass
    pos_m = re.search(r"position\W{0,4}(\d+(?:\.\d+)?)", text, re.I)
    if pos_m:
        conf_m = re.search(r"confidence\W{0,4}(\d)", text, re.I)
        return (max(0.0, min(10.0, float(pos_m.group(1)))),
                int(conf_m.group(1)) if conf_m else None)
    return None, None
