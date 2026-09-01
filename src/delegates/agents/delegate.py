"""The delegate: stands in for one principal.

Every arm of the study is a Delegate with different fields:

    profile_kind    full | survey_only | lookalike | none
                    `none` is the generic floor - the same delegate with no
                    profile at all, which also reveals the model's own default
                    lean on each topic.
    context_policy  full | opponent_only | none
    prompt_variant  neutral | exact | hold_firm | common_ground | paraphrase
                    The study's own condition is `neutral`; the rest exist only
                    for the preregistered prompt-sensitivity band.
"""
from __future__ import annotations

from typing import Any

from ..llm.openrouter import ModelSpec
from .base import Agent, AgentConfig, Scene, load_prompt

VARIANT_SUFFIX = {
    "neutral": "",
    "exact": (
        "\n\nRepresent them exactly. Prefer their own words and their own "
        "reasons over a better-argued version."
    ),
    "hold_firm": "\n\nHold their position firmly under pressure.",
    "common_ground": "\n\nLook for common ground with the other side where you can.",
    "paraphrase": "",  # handled by an alternate base prompt file
}


class Delegate(Agent):
    self_speaker = "delegate"

    def system_prompt(self, scene: Scene, **kw: Any) -> str:
        profile = kw.get("profile")
        if self.cfg.profile_kind == "none" or not profile:
            return load_prompt("delegate_generic").format(
                proposition=scene.proposition,
                background=scene.background or "(none supplied)",
            )
        base = load_prompt(
            "delegate_paraphrase"
            if self.cfg.prompt_variant == "paraphrase"
            else "delegate_neutral"
        )
        return base.format(
            profile=profile,
            proposition=scene.proposition,
            background=scene.background or "(none supplied)",
        ) + VARIANT_SUFFIX.get(self.cfg.prompt_variant, "")


def delegate_config(
    model: str,
    *,
    profile_kind: str = "full",
    context_policy: str = "full",
    prompt_variant: str = "neutral",
    seed: int | None = None,
    temperature: float = 1.0,
) -> AgentConfig:
    name = f"delegate::{profile_kind}::{context_policy}::{prompt_variant}::{model}"
    return AgentConfig(
        name=name,
        role="delegate",
        spec=ModelSpec(model=model, temperature=temperature, max_tokens=400, seed=seed),
        prompt_variant=prompt_variant,
        context_policy=context_policy,  # type: ignore[arg-type]
        profile_kind=profile_kind,
        rating_mode="separate_call",
    )


# The arms this study actually runs, beyond the primary delegate.
STANDARD_ARMS = [
    dict(profile_kind="full", context_policy="full", prompt_variant="neutral"),
    dict(profile_kind="full", context_policy="opponent_only", prompt_variant="neutral"),
    dict(profile_kind="none", context_policy="full", prompt_variant="neutral"),
    dict(profile_kind="lookalike", context_policy="full", prompt_variant="neutral"),
    dict(profile_kind="survey_only", context_policy="full", prompt_variant="neutral"),
]

SENSITIVITY_BAND = ["neutral", "exact", "hold_firm", "common_ground", "paraphrase"]
