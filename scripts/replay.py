"""Teacher-forced delegate replay of a frozen principal debate.

For each delegate arm, walk the principal's frozen transcript exchange by
exchange: copy the interlocutor turn verbatim, let the delegate reply in the
principal's place, then elicit the delegate's rating in a separate call. The
delegate's `context_policy` decides whose earlier replies it sees:

    full            the principal's actual prior replies
    opponent_only   the delegate's own prior replies
    none            only the current interlocutor turn

Each arm is a `debates` row with actor='delegate', mode='replay_*' and
source_debate_id pointing at the principal run. Every delegate turn carries
source_turn_id = the principal turn it stood in for, and delegate ratings use
the same after_turn_idx as the principal's, so comparison is a join.

    uv run python scripts/replay.py --dry           # stubbed model, temp copy of the db
    uv run python scripts/replay.py                 # latest frozen principal debate, live
    uv run python scripts/replay.py --debate 2 --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MODE_FOR_POLICY = {
    "full": "replay_full",
    "opponent_only": "replay_opponent_only",
    "none": "replay_none",
}


def profile_for(conn, participant_id: int, kind: str):
    """The profiles row a delegate of this kind is conditioned on, or None.

    `full` is the frozen Session 1 profile. `survey_only` is derived from it
    once (the structured fields, no free text) and stored as its own row so
    the released data shows exactly what each arm saw. `lookalike` needs
    donor profiles that do not exist yet.
    """
    from delegates.db import insert, js, one, unjs

    if kind == "none":
        return None
    row = one(
        conn,
        "SELECT * FROM profiles WHERE participant_id=? AND kind=? "
        "AND frozen_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (participant_id, kind),
    )
    if row or kind != "survey_only":
        return row
    full = one(
        conn,
        "SELECT * FROM profiles WHERE participant_id=? AND kind='full' "
        "AND frozen_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (participant_id,),
    )
    if not full:
        return None
    from delegates.web.app import PROFILE_FIELDS

    payload = unjs(full["payload_json"], {}) or {}
    survey = {k: payload.get(k) for k, _, _ in PROFILE_FIELDS}
    rendered = "\n".join(
        f"- {label}: {survey.get(key) or '(not given)'}" for key, label, _ in PROFILE_FIELDS
    )
    pid = insert(
        conn, "profiles", participant_id=participant_id, kind="survey_only",
        payload_json=js(survey), rendered=rendered, frozen_at=full["frozen_at"],
    )
    return one(conn, "SELECT * FROM profiles WHERE id=?", (pid,))


def pairs(turns):
    """(interlocutor turn, principal turn) per exchange, in order."""
    out, pending = [], None
    for t in turns:
        if t.speaker == "interlocutor":
            pending = t
        elif t.speaker == "principal" and pending is not None:
            out.append((pending, t))
            pending = None
    return out


async def replay_arm(conn, source, arm: dict, *, model: str, run_index: int) -> int | None:
    from delegates import study
    from delegates.agents.base import Scene, Turn
    from delegates.agents.delegate import Delegate, delegate_config
    from delegates.db import insert, js, one, unjs

    profile = profile_for(conn, source["participant_id"], arm["profile_kind"])
    if arm["profile_kind"] not in ("none",) and profile is None:
        print(f"  skip {arm}: no {arm['profile_kind']} profile for this participant")
        return None

    cfg = delegate_config(model, seed=(source["seed"] or 0) + run_index, **arm)
    cfg_id = study.ensure_agent_config(conn, cfg)
    mode = MODE_FOR_POLICY[arm["context_policy"]]

    existing = one(
        conn,
        "SELECT * FROM debates WHERE source_debate_id=? AND delegate_cfg_id=? "
        "AND run_index=? ORDER BY id DESC LIMIT 1",
        (source["id"], cfg_id, run_index),
    )
    if existing and existing["status"] == "frozen":
        print(f"  {cfg.name} run {run_index}: already frozen as debate {existing['id']}")
        return existing["id"]
    if existing:
        conn.execute("UPDATE debates SET status='abandoned' WHERE id=?", (existing["id"],))

    plan = {"n_exchanges": len(pairs(study.transcript(conn, source["id"]))),
            "source_plan": unjs(source["plan_json"], {})}
    did = insert(
        conn, "debates", participant_id=source["participant_id"],
        topic_id=source["topic_id"], actor="delegate", mode=mode,
        source_debate_id=source["id"], interlocutor_cfg_id=source["interlocutor_cfg_id"],
        delegate_cfg_id=cfg_id, profile_id=profile["id"] if profile else None,
        plan_json=js(plan), seed=cfg.spec.seed, run_index=run_index,
    )
    debate = one(conn, "SELECT * FROM debates WHERE id=?", (did,))
    topic = study.topic_of(conn, debate)
    agent = Delegate(cfg)
    rendered = profile["rendered"] if profile else None

    history: list[Turn] = []   # what the delegate is shown, before policy filtering
    print(f"  {cfg.name} run {run_index} -> debate {did} ({mode})")

    for k, (opp, principal) in enumerate(pairs(study.transcript(conn, source["id"]))):
        conn.execute("BEGIN")
        opp_id = study.append_turn(
            conn, did, "interlocutor", opp.content,
            constraint_cell=opp.constraint_cell, source_turn_id=opp.id,
        )
        conn.execute("COMMIT")
        history.append(Turn(idx=opp.idx, speaker="interlocutor", content=opp.content, id=opp_id))
        scene = Scene(
            proposition=topic["proposition"], background=topic["background"] or "",
            history=list(history), low_label=topic["scale_low_label"],
            high_label=topic["scale_high_label"],
        )

        conn.execute("BEGIN")
        out = await agent.speak(scene, conn=conn, debate_id=did, profile=rendered)
        turn_id = study.append_turn(
            conn, did, "delegate", out.content, source_turn_id=principal.id,
            model_call_id=out.call_id, ms_elapsed=out.latency_ms,
        )
        pos, conf, raw = await agent.rate(scene, out.content, conn=conn, debate_id=did)
        if pos is not None:
            study.record_rating(
                conn, did, principal.idx, pos, conf, source="agent_reported", raw=raw,
            )
        conn.execute("COMMIT")

        # Teacher-force the next exchange: the principal's real reply under
        # `full`, the delegate's own under anything else.
        if arm["context_policy"] == "full":
            history.append(Turn(idx=principal.idx, speaker="principal",
                                content=principal.content, id=principal.id))
        else:
            history.append(Turn(idx=principal.idx, speaker="delegate",
                                content=out.content, id=turn_id))
        print(f"    ex {k}: pos={pos} conf={conf}  {out.content[:70]!r}")

    conn.execute("BEGIN")
    study.freeze(conn, did)
    conn.execute("COMMIT")
    return did


async def rerate(conn, did: int) -> None:
    """Re-elicit ratings for an existing delegate debate, keeping its turns."""
    from delegates import study
    from delegates.agents.base import Scene, Turn
    from delegates.agents.delegate import Delegate, delegate_config
    from delegates.db import one, unjs

    debate = one(conn, "SELECT * FROM debates WHERE id=?", (did,))
    cfg_row = one(conn, "SELECT * FROM agent_configs WHERE id=?", (debate["delegate_cfg_id"],))
    params = unjs(cfg_row["params_json"], {}) or {}
    cfg = delegate_config(
        cfg_row["model"], profile_kind=cfg_row["profile_kind"],
        context_policy=cfg_row["context_policy"], prompt_variant=cfg_row["prompt_variant"],
        seed=debate["seed"], temperature=params.get("temperature", 1.0),
    )
    cfg.id = cfg_row["id"]
    agent = Delegate(cfg)
    topic = study.topic_of(conn, debate)
    source_turns = {t.id: t for t in study.transcript(conn, debate["source_debate_id"])}
    history: list[Turn] = []
    print(f"  re-rating debate {did} ({cfg.name})")
    for t in study.transcript(conn, did):
        if t.speaker == "interlocutor":
            history.append(t)
            continue
        scene = Scene(
            proposition=topic["proposition"], background=topic["background"] or "",
            history=list(history), low_label=topic["scale_low_label"],
            high_label=topic["scale_high_label"],
        )
        conn.execute("BEGIN")
        pos, conf, raw = await agent.rate(scene, t.content, conn=conn, debate_id=did)
        conn.execute("DELETE FROM ratings WHERE debate_id=? AND after_turn_idx=? "
                     "AND source='agent_reported'", (did, t.idx))
        if pos is not None:
            study.record_rating(conn, did, t.idx, pos, conf, source="agent_reported", raw=raw)
        conn.execute("COMMIT")
        if cfg.context_policy == "full":
            src = source_turns.get(t.source_turn_id)
            history.append(Turn(idx=t.idx, speaker="principal", content=src.content if src else t.content))
        else:
            history.append(t)
        print(f"    turn {t.idx}: pos={pos} conf={conf}")


def summarise(conn, source_id: int, delegate_ids: list[int]) -> None:
    from delegates.db import all_rows, one

    cols = [("principal", source_id, "participant")] + [
        (one(conn, "SELECT name FROM agent_configs WHERE id="
             "(SELECT delegate_cfg_id FROM debates WHERE id=?)", (d,))["name"]
         .split("::", 1)[1].rsplit("::", 1)[0] + f"#{one(conn, 'SELECT run_index FROM debates WHERE id=?', (d,))['run_index']}",
         d, "agent_reported")
        for d in delegate_ids
    ]
    series = {}
    for label, did, src in cols:
        series[label] = {
            r["after_turn_idx"]: r["position"]
            for r in all_rows(conn, "SELECT after_turn_idx, position FROM ratings "
                                    "WHERE debate_id=? AND source=?", (did, src))
        }
    idxs = sorted({i for s in series.values() for i in s})
    labels = list(series)
    width = max(len(l) for l in labels)
    print("\nposition after turn:")
    print(" " * (width + 2) + "".join(f"{i:>6}" for i in idxs))
    for l in labels:
        row = "".join(
            f"{series[l][i]:>6.1f}" if i in series[l] else f"{'-':>6}" for i in idxs
        )
        print(f"  {l:<{width}}{row}")
    principal = series["principal"]
    print("\nmean |delegate - principal| over shared turns:")
    for l in labels[1:]:
        shared = [i for i in series[l] if i in principal]
        if shared:
            mad = sum(abs(series[l][i] - principal[i]) for i in shared) / len(shared)
            print(f"  {l:<{width}}  {mad:.2f}  (n={len(shared)})")
    cost = one(conn, "SELECT round(sum(cost_usd),4) c, count(*) n FROM model_calls "
                     f"WHERE debate_id IN ({','.join('?'*len(delegate_ids))})",
               tuple(delegate_ids))
    print(f"\nmodel calls: {cost['n']}, cost ${cost['c'] or 0}")


async def run(args) -> int:
    from delegates import study
    from delegates.agents.delegate import STANDARD_ARMS
    from delegates.db import all_rows, connect, one
    from delegates.llm.registry import PINNED

    if args.dry:
        from dry_run import stub_client
        stub_client()

    conn = connect()
    try:
        if args.debate:
            source = one(conn, "SELECT * FROM debates WHERE id=?", (args.debate,))
        else:
            source = one(conn, "SELECT * FROM debates WHERE actor='principal' AND "
                               "status='frozen' ORDER BY frozen_at DESC LIMIT 1")
        if not source:
            print("no frozen principal debate found")
            return 1
        if source["status"] != "frozen" or source["actor"] != "principal":
            print(f"debate {source['id']} is {source['actor']}/{source['status']}, "
                  "need a frozen principal run")
            return 1
        n = len(pairs(study.transcript(conn, source["id"])))
        print(f"source debate {source['id']}: participant {source['participant_id']}, "
              f"topic {source['topic_id']}, {n} exchanges")

        if args.rerate:
            ids = [r["id"] for r in all_rows(
                conn, "SELECT id FROM debates WHERE source_debate_id=? AND actor='delegate' "
                      "AND status='frozen' ORDER BY id", (source["id"],))]
            for did in ids:
                await rerate(conn, did)
            summarise(conn, source["id"], ids)
            return 0

        arms = [a for a in STANDARD_ARMS
                if not args.arms or a["profile_kind"] + "/" + a["context_policy"] in args.arms]
        model = args.model or PINNED["delegate"]
        done: list[int] = []
        for run_index in range(args.runs):
            for arm in arms:
                did = await replay_arm(conn, source, arm, model=model, run_index=run_index)
                if did:
                    done.append(did)
        if done:
            summarise(conn, source["id"], done)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debate", type=int, help="principal debate id (default: latest frozen)")
    ap.add_argument("--runs", type=int, default=1, help="repeats per arm, for the noise floor")
    ap.add_argument("--arms", nargs="*", help="subset, as profile_kind/context_policy")
    ap.add_argument("--model", help="override the delegate model")
    ap.add_argument("--rerate", action="store_true",
                    help="re-elicit ratings for existing delegate debates, keep their turns")
    ap.add_argument("--dry", action="store_true",
                    help="stub the model and work on a temp copy of the db")
    args = ap.parse_args()
    if args.dry:
        # Resolve the db path without importing delegates.config, which would
        # pin DB_PATH before the override below is in place.
        src_db = Path(os.getenv("DELEGATES_DB", ROOT / "data" / "delegates.db"))
        if not src_db.is_absolute():
            src_db = ROOT / src_db
        tmp = Path(tempfile.mkdtemp()) / "replay.db"
        shutil.copy(src_db, tmp)
        os.environ["DELEGATES_DB"] = str(tmp)
        os.environ["DELEGATES_JOURNAL_MODE"] = "truncate"
        print(f"dry run on copy: {tmp}")
    sys.exit(asyncio.run(run(args)))
