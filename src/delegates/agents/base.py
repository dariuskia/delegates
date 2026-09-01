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
            role = "assistant" if turn.speaker == self.self_speaker else "user"
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
            model=self.cfg.spec.model, temperature=0.0, max_tokens=60,
            seed=self.cfg.spec.seed,
        )
        out = await self.client.complete(
            spec,
            [{"role": "user", "content": prompt}],
            conn=conn,
            purpose="rating",
            debate_id=debate_id,
            agent_config_id=self.cfg.id,
        )
        pos, conf = parse_rating(out.content)
        return pos, conf, out.content


def parse_rating(text: str) -> tuple[float | None, int | None]:
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            pos = float(data["position"])
            conf = int(data["confidence"]) if data.get("confidence") is not None else None
            return max(0.0, min(10.0, pos)), conf
        except (ValueError, KeyError, TypeError):
            pass
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if nums:
        return max(0.0, min(10.0, float(nums[0]))), None
    return None, None
