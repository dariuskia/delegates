"""Study service layer: enrolment, debate state machine, persistence.

The web app holds no study logic of its own - it renders whatever stage this
module reports. Anything that drives a debate (the live UI, the synthetic-
principal dry run, later the replay harness) goes through the same functions,
so the frozen record has one shape regardless of who produced it.
"""
from __future__ import annotations

import random
import secrets
import sqlite3
from typing import Any

from .agents.base import Scene, Turn
from .agents.interlocutor import Interlocutor, build_plan, cell_for, interlocutor_config
from .agents.interlocutor import opposing_stance
from .config import N_EXCHANGES
from .db import all_rows, insert, js, one, unjs
from .llm.registry import PINNED

OPENING_RATING_IDX = -1


# ------------------------------------------------------------- enrolment

def enroll(conn: sqlite3.Connection, code: str, cohort: str = "pilot") -> sqlite3.Row:
    existing = one(conn, "SELECT * FROM participants WHERE code = ?", (code,))
    if existing:
        return existing
    topics = all_rows(conn, "SELECT id FROM topics WHERE active = 1 ORDER BY id")
    if len(topics) < 2:
        raise RuntimeError("need at least two active topics before enrolling")
    rng = random.Random(f"{code}:{cohort}")
    debated, cold = rng.sample([t["id"] for t in topics], 2)
    pid = insert(
        conn, "participants", code=code, cohort=cohort,
        debated_topic_id=debated, cold_topic_id=cold,
    )
    for n in (1, 2, 3):
        insert(
            conn, "study_sessions", participant_id=pid, session_no=n,
            token=secrets.token_urlsafe(16),
        )
    return one(conn, "SELECT * FROM participants WHERE id = ?", (pid,))


def session_token(conn: sqlite3.Connection, participant_id: int, n: int) -> str:
    row = one(
        conn,
        "SELECT token FROM study_sessions WHERE participant_id = ? AND session_no = ?",
        (participant_id, n),
    )
    return row["token"] if row else ""


def participant_for_token(conn: sqlite3.Connection, token: str) -> tuple[sqlite3.Row, int] | None:
    row = one(conn, "SELECT * FROM study_sessions WHERE token = ?", (token,))
    if not row:
        return None
    p = one(conn, "SELECT * FROM participants WHERE id = ?", (row["participant_id"],))
    return (p, row["session_no"]) if p else None


# ---------------------------------------------------------------- debates

def ensure_agent_config(conn: sqlite3.Connection, cfg) -> int:
    row = one(conn, "SELECT id FROM agent_configs WHERE name = ?", (cfg.name,))
    if row:
        cfg.id = row["id"]
        return row["id"]
    cfg.id = insert(
        conn, "agent_configs", name=cfg.name, role=cfg.role, model=cfg.spec.model,
        params_json=js({
            "temperature": cfg.spec.temperature,
            "top_p": cfg.spec.top_p,
            "max_tokens": cfg.spec.max_tokens,
        }),
        prompt_variant=cfg.prompt_variant, context_policy=cfg.context_policy,
        profile_kind=cfg.profile_kind, rating_mode=cfg.rating_mode,
    )
    return cfg.id


def start_live_debate(
    conn: sqlite3.Connection,
    participant: sqlite3.Row,
    *,
    n_exchanges: int = N_EXCHANGES,
    model: str | None = None,
) -> sqlite3.Row:
    existing = one(
        conn,
        "SELECT * FROM debates WHERE participant_id = ? AND actor = 'principal' "
        "AND mode = 'live' ORDER BY id DESC LIMIT 1",
        (participant["id"],),
    )
    if existing:
        return existing
    seed = random.Random(participant["code"]).randrange(1, 2**31)
    cfg = interlocutor_config(model or PINNED["interlocutor"])
    cfg_id = ensure_agent_config(conn, cfg)
    did = insert(
        conn, "debates", participant_id=participant["id"],
        topic_id=participant["debated_topic_id"], actor="principal", mode="live",
        interlocutor_cfg_id=cfg_id, plan_json=js(build_plan(seed, n_exchanges)),
        seed=seed,
    )
    return one(conn, "SELECT * FROM debates WHERE id = ?", (did,))


def topic_of(conn: sqlite3.Connection, debate: sqlite3.Row) -> sqlite3.Row:
    return one(conn, "SELECT * FROM topics WHERE id = ?", (debate["topic_id"],))


def transcript(conn: sqlite3.Connection, debate_id: int) -> list[Turn]:
    rows = all_rows(
        conn, "SELECT * FROM turns WHERE debate_id = ? ORDER BY idx", (debate_id,)
    )
    return [
        Turn(
            idx=r["idx"], speaker=r["speaker"], content=r["content"], id=r["id"],
            act_type=r["act_type"], constraint_cell=r["constraint_cell"],
            source_turn_id=r["source_turn_id"],
        )
        for r in rows
    ]


def scene_for(conn: sqlite3.Connection, debate: sqlite3.Row) -> Scene:
    topic = topic_of(conn, debate)
    return Scene(
        proposition=topic["proposition"],
        background=topic["background"] or "",
        history=transcript(conn, debate["id"]),
        low_label=topic["scale_low_label"],
        high_label=topic["scale_high_label"],
    )


def ratings_of(conn: sqlite3.Connection, debate_id: int, source: str = "participant"):
    return all_rows(
        conn,
        "SELECT * FROM ratings WHERE debate_id = ? AND source = ? "
        "ORDER BY after_turn_idx",
        (debate_id, source),
    )


# ---------------------------------------------------------- state machine

def stage(conn: sqlite3.Connection, debate: sqlite3.Row) -> str:
    """One of: opening_rating | await_interlocutor | reply | rating | complete."""
    plan = unjs(debate["plan_json"], {}) or {}
    n_exchanges = plan.get("n_exchanges", N_EXCHANGES)
    turns = transcript(conn, debate["id"])
    n_int = sum(1 for t in turns if t.speaker == "interlocutor")
    n_self = sum(1 for t in turns if t.speaker in ("principal", "delegate"))
    rated = {r["after_turn_idx"] for r in ratings_of(conn, debate["id"])}

    if OPENING_RATING_IDX not in rated:
        return "opening_rating"
    if n_self and turns:
        last_self = max(t.idx for t in turns if t.speaker in ("principal", "delegate"))
        if last_self not in rated:
            return "rating"
    if n_self >= n_exchanges:
        return "complete"
    if n_int == n_self:
        return "await_interlocutor"
    return "reply"


def current_exchange(conn: sqlite3.Connection, debate_id: int) -> int:
    turns = transcript(conn, debate_id)
    return sum(1 for t in turns if t.speaker == "interlocutor")


# -------------------------------------------------------------- mutations

def record_rating(
    conn: sqlite3.Connection, debate_id: int, after_turn_idx: int,
    position: float, confidence: int | None, ms_elapsed: int | None = None,
    source: str = "participant", raw: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ratings "
        "(debate_id, after_turn_idx, position, confidence, source, ms_elapsed, raw) "
        "VALUES (?,?,?,?,?,?,?)",
        (debate_id, after_turn_idx, position, confidence, source, ms_elapsed, raw),
    )


def append_turn(
    conn: sqlite3.Connection, debate_id: int, speaker: str, content: str,
    *, act_type: str | None = None, constraint_cell: str | None = None,
    source_turn_id: int | None = None, model_call_id: int | None = None,
    ms_elapsed: int | None = None,
) -> int:
    row = one(conn, "SELECT MAX(idx) AS m FROM turns WHERE debate_id = ?", (debate_id,))
    idx = 0 if row["m"] is None else row["m"] + 1
    return insert(
        conn, "turns", debate_id=debate_id, idx=idx, speaker=speaker,
        content=content, act_type=act_type, constraint_cell=constraint_cell,
        source_turn_id=source_turn_id, model_call_id=model_call_id,
        ms_elapsed=ms_elapsed,
    )


def stance_of(conn: sqlite3.Connection, debate: sqlite3.Row) -> str:
    """Fixed for the whole debate, from the opening rating, stored in the plan."""
    plan = unjs(debate["plan_json"], {}) or {}
    if plan.get("stance"):
        return plan["stance"]
    opening = one(
        conn,
        "SELECT position FROM ratings WHERE debate_id = ? AND after_turn_idx = ? "
        "AND source = 'participant'",
        (debate["id"], OPENING_RATING_IDX),
    )
    stance = opposing_stance(opening["position"] if opening else 5.0)
    plan["stance"] = stance
    conn.execute("UPDATE debates SET plan_json = ? WHERE id = ?", (js(plan), debate["id"]))
    return stance


async def generate_interlocutor_turn(
    conn: sqlite3.Connection, debate: sqlite3.Row
) -> int:
    plan = unjs(debate["plan_json"], {}) or {}
    exchange = current_exchange(conn, debate["id"])
    cell = cell_for(plan, exchange)
    cfg = interlocutor_config(
        one(conn, "SELECT model FROM agent_configs WHERE id = ?",
            (debate["interlocutor_cfg_id"],))["model"]
    )
    cfg.id = debate["interlocutor_cfg_id"]
    agent = Interlocutor(cfg)
    scene = scene_for(conn, debate)
    out = await agent.speak(
        scene, conn=conn, debate_id=debate["id"],
        stance=stance_of(conn, debate), constraint_cell=cell,
    )
    return append_turn(
        conn, debate["id"], "interlocutor", out.content,
        constraint_cell=cell, model_call_id=out.call_id, ms_elapsed=out.latency_ms,
    )


def freeze(conn: sqlite3.Connection, debate_id: int) -> None:
    conn.execute(
        "UPDATE debates SET status = 'frozen', frozen_at = datetime('now') "
        "WHERE id = ? AND status != 'frozen'",
        (debate_id,),
    )


def log_event(
    conn: sqlite3.Connection, kind: str, *, participant_id: int | None = None,
    debate_id: int | None = None, **payload: Any
) -> None:
    insert(
        conn, "events", participant_id=participant_id, debate_id=debate_id,
        kind=kind, payload_json=js(payload),
    )


# ------------------------------------------------- session 3: authorization
# The participant sees delegate acts one at a time, blind to which delegate
# produced them, and says whether they would have authorised each to be said
# on their behalf. `kind` records the provenance for analysis; the page never
# shows it. Items are built once per participant from a seeded shuffle.

N_GENERIC_BLIND = 4
N_REPEAT = 2


def review_items_of(conn: sqlite3.Connection, participant_id: int) -> list[sqlite3.Row]:
    return all_rows(
        conn,
        "SELECT * FROM review_items WHERE participant_id = ? ORDER BY order_idx",
        (participant_id,),
    )


def _delegate_turns(conn: sqlite3.Connection, participant_id: int, profile_kind: str,
                    context_policy: str = "full") -> list[sqlite3.Row]:
    return all_rows(conn, """
        SELECT t.* FROM turns t
        JOIN debates d ON d.id = t.debate_id
        JOIN agent_configs c ON c.id = d.delegate_cfg_id
        WHERE d.participant_id = ? AND d.actor = 'delegate' AND d.status = 'frozen'
          AND c.profile_kind = ? AND c.context_policy = ? AND c.prompt_variant = 'neutral'
          AND t.speaker = 'delegate' AND d.run_index = 0
        ORDER BY d.id DESC, t.idx
    """, (participant_id, profile_kind, context_policy))


def _position_delta(conn: sqlite3.Connection, turn: sqlite3.Row) -> float | None:
    rows = all_rows(
        conn,
        "SELECT after_turn_idx, position FROM ratings WHERE debate_id = ? "
        "AND source = 'agent_reported' AND after_turn_idx <= ? ORDER BY after_turn_idx DESC LIMIT 2",
        (turn["debate_id"], turn["idx"]),
    )
    if len(rows) == 2 and rows[0]["after_turn_idx"] == turn["idx"]:
        return rows[0]["position"] - rows[1]["position"]
    return None


def build_review_items(conn: sqlite3.Connection, participant: sqlite3.Row) -> list[sqlite3.Row]:
    existing = review_items_of(conn, participant["id"])
    if existing:
        return existing
    own = _delegate_turns(conn, participant["id"], "full")
    own = [t for t in own if t["debate_id"] == own[0]["debate_id"]] if own else []
    generic = _delegate_turns(conn, participant["id"], "none")
    generic = [t for t in generic if t["debate_id"] == generic[0]["debate_id"]] if generic else []
    lookalike = _delegate_turns(conn, participant["id"], "lookalike")
    if not own:
        raise RuntimeError("no frozen primary delegate replay for this participant")

    rng = random.Random(f"review:{participant['code']}")
    items = [("own", t) for t in own]
    items += [("generic_blind", t) for t in rng.sample(generic, min(N_GENERIC_BLIND, len(generic)))]
    items += [("lookalike_blind", t) for t in rng.sample(lookalike, min(N_GENERIC_BLIND, len(lookalike)))]
    rng.shuffle(items)
    # Repeats go in the second half so they are never adjacent to the original.
    for t in rng.sample(own, min(N_REPEAT, len(own))):
        items.insert(rng.randrange(len(items) // 2, len(items) + 1), ("repeat", t))

    for i, (kind, t) in enumerate(items):
        insert(
            conn, "review_items", participant_id=participant["id"], turn_id=t["id"],
            kind=kind, order_idx=i, shown_position_delta=_position_delta(conn, t),
        )
    return review_items_of(conn, participant["id"])


def next_review_item(conn: sqlite3.Connection, participant_id: int) -> sqlite3.Row | None:
    return one(conn, """
        SELECT r.* FROM review_items r
        LEFT JOIN authorizations a ON a.review_item_id = r.id
        WHERE r.participant_id = ? AND a.id IS NULL
        ORDER BY r.order_idx LIMIT 1
    """, (participant_id,))


def review_item_context(conn: sqlite3.Connection, item: sqlite3.Row) -> dict[str, Any]:
    """What the participant is shown: the interlocutor turn answered, the
    delegate's reply, and the delegate's reported position before and after."""
    turn = one(conn, "SELECT * FROM turns WHERE id = ?", (item["turn_id"],))
    prompt = one(
        conn, "SELECT * FROM turns WHERE debate_id = ? AND idx < ? AND speaker = 'interlocutor' "
              "ORDER BY idx DESC LIMIT 1", (turn["debate_id"], turn["idx"]),
    )
    after = one(conn, "SELECT position FROM ratings WHERE debate_id = ? AND after_turn_idx = ? "
                      "AND source = 'agent_reported'", (turn["debate_id"], turn["idx"]))
    before = one(conn, "SELECT position FROM ratings WHERE debate_id = ? AND after_turn_idx < ? "
                       "AND source = 'agent_reported' ORDER BY after_turn_idx DESC LIMIT 1",
                 (turn["debate_id"], turn["idx"]))
    return {
        "item": item, "turn": turn, "prompt": prompt,
        "before": before["position"] if before else None,
        "after": after["position"] if after else None,
    }


def record_authorization(
    conn: sqlite3.Connection, review_item_id: int, verdict: str,
    comment: str | None, ms_elapsed: int | None,
) -> None:
    if verdict not in ("yes", "reservations", "no"):
        raise ValueError(f"bad verdict: {verdict}")
    if one(conn, "SELECT 1 FROM authorizations WHERE review_item_id = ?", (review_item_id,)):
        return
    insert(
        conn, "authorizations", review_item_id=review_item_id, verdict=verdict,
        comment=(comment or "").strip() or None, ms_elapsed=ms_elapsed,
    )
