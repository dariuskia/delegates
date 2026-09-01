import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delegates.agents.base import Agent, AgentConfig, Scene, Turn, parse_rating
from delegates.agents.interlocutor import build_plan, cell_for, opposing_stance
from delegates.llm.openrouter import ModelSpec


def cfg(policy):
    return AgentConfig(name="t", role="delegate", spec=ModelSpec(model="m"),
                       context_policy=policy)


HISTORY = [
    Turn(0, "interlocutor", "A"),
    Turn(1, "principal", "B"),
    Turn(2, "interlocutor", "C"),
]
SCENE = Scene(proposition="P", background="", history=HISTORY)


def test_context_policy_full_sees_principal_replies():
    assert len(Agent(cfg("full")).visible(SCENE)) == 3


def test_context_policy_opponent_only_withholds_principal_replies():
    seen = Agent(cfg("opponent_only")).visible(SCENE)
    assert [t.speaker for t in seen] == ["interlocutor", "interlocutor"]


def test_context_policy_none_sees_only_latest_opponent_turn():
    seen = Agent(cfg("none")).visible(SCENE)
    assert len(seen) == 1 and seen[0].content == "C"


def test_plan_assigns_each_cell_once_and_is_seed_stable():
    a, b = build_plan(7, 8), build_plan(7, 8)
    assert a == b
    cells = [c for c in a["cells"].values() if c != "free"]
    assert sorted(cells) == sorted(set(cells)) and len(cells) == 4
    assert cell_for(a, 0) == "free"


def test_stance_opposes_the_participant():
    assert opposing_stance(8.0) == "against"
    assert opposing_stance(2.0) == "for"


def test_rating_parser():
    assert parse_rating('{"position": 7.5, "confidence": 3}') == (7.5, 3)
    assert parse_rating("about 4 out of 10")[0] == 4.0
    assert parse_rating('{"position": 99, "confidence": 2}')[0] == 10.0
    assert parse_rating("no numbers here") == (None, None)
