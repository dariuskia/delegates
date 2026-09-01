"""End-to-end dry run: a synthetic principal walks the whole of Session 1.

This is pilot step 3 in miniature. It drives the real HTTP routes through an
in-process ASGI transport, so the state machine, templates and persistence are
all exercised exactly as a participant would exercise them.

    uv run python scripts/dry_run.py            # stubbed model, no API key
    uv run python scripts/dry_run.py --live     # real OpenRouter calls
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def stub_client():
    """Deterministic canned interlocutor turns, still logged to model_calls."""
    from delegates.llm.openrouter import Completion, OpenRouter, cache_key

    lines = [
        "You say people are being priced out, but the places with the strictest "
        "limits are the places where the least gets built. That is the part I "
        "think your view has to answer.",
        "Consider who actually benefits. The protection goes to whoever already "
        "holds the lease, not to the person looking for somewhere to live now.",
        "Anyone who has looked at this seriously understands that price controls "
        "produce shortages. That is not really in dispute.",
        "One study of a 1994 expansion found rental supply falling by around "
        "fifteen percent among affected buildings over the following decade.",
        "I may be wrong about this, but I wonder whether the effect is as large "
        "as either of us is assuming.",
        "The evidence here is fairly clear and I think it settles the question: "
        "affected landlords converted or sold rather than kept renting.",
        "Even granting your point about displacement, the policy still has to be "
        "compared against alternatives that do not restrict supply.",
        "So where does that leave you? The mechanism you are relying on is the "
        "one the record does not support.",
    ]
    counter = {"n": 0}

    async def complete(self, spec, messages, *, conn=None, purpose="turn",
                       debate_id=None, agent_config_id=None, attempts=4):
        if purpose == "rating":
            content = '{"position": 5, "confidence": 3}'
        else:
            content = lines[counter["n"] % len(lines)]
            counter["n"] += 1
        payload = spec.payload(messages)
        call_id = OpenRouter._log(
            conn, purpose, spec, payload, {"usage": {}}, cache_key(payload),
            debate_id, agent_config_id, 1, content=content,
        )
        return Completion(content, spec.model, call_id, 0, 0, 0.0, 1, {})

    OpenRouter.complete = complete  # type: ignore[method-assign]


REPLIES = [
    "The supply argument assumes the only thing that matters is how much gets "
    "built. People being forced out of the neighbourhood they grew up in is a "
    "real cost too, and it happens now, not in fifteen years.",
    "Sure, but the person looking for a place today was a tenant somewhere "
    "yesterday. Protection is not a fixed group of people, it rotates.",
    "I do not find 'everyone serious agrees' persuasive on its own. Plenty of "
    "things everyone serious agreed about turned out to be wrong.",
    "That is a real number and it does give me pause. Fifteen percent is a lot "
    "more than I would have guessed.",
    "If you are unsure about the size of the effect then I think that cuts "
    "toward letting cities try it and measure what happens.",
    "I still think you are comparing against an idealised alternative that no "
    "one is actually offering.",
    "Fine, but the alternatives that do not restrict supply have been available "
    "the whole time and have not been used.",
    "I have moved somewhat on the supply point. I have not moved on who bears "
    "the cost in the meantime.",
]

POSITIONS = [8, 8, 7, 7, 6, 6, 6, 5, 5]


async def run(live: bool) -> int:
    import httpx
    from delegates.db import connect, init_db, one

    # ASGITransport does not fire startup events, so build the schema here.
    init_db()
    import seed as seed_mod  # noqa: E402
    conn = connect()
    try:
        for t in seed_mod.TOPICS:
            if one(conn, "SELECT 1 FROM topics WHERE slug = ?", (t["slug"],)):
                continue
            cols = ", ".join(t)
            marks = ", ".join("?" for _ in t)
            conn.execute(f"INSERT INTO topics ({cols}) VALUES ({marks})", tuple(t.values()))
    finally:
        conn.close()

    if not live:
        stub_client()

    from delegates.web.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", follow_redirects=True
    ) as c:
        assert (await c.get("/")).status_code == 200
        r = await c.post("/start", data={"code": "DRYRUN-001", "cohort": "dryrun"})
        assert r.status_code == 200, r.status_code
        r = await c.post("/consent", data={"agree": "yes", "recontact": "on"})
        assert r.status_code == 200
        r = await c.post("/profile", data={
            "age_band": "25-34", "politics": "Centre-left", "openness": "Somewhat",
            "mind_change": "I used to think congestion pricing was regressive.",
        })
        assert r.status_code == 200, r.status_code

        step = 0
        for _ in range(60):
            r = await c.get("/debate/view")
            body = r.text
            if "Finish this session" in body:
                break
            if 'data-advance' in body:
                await c.post("/debate/advance")
            elif 'name="content"' in body:
                await c.post("/debate/reply", data={
                    "content": REPLIES[min(step, len(REPLIES) - 1)],
                    "ms_elapsed": 41000,
                })
                step += 1
            elif 'id="rating-form"' in body:
                idx = -1 if step == 0 else (step * 2 - 1)
                await c.post("/debate/rating", data={
                    "position": POSITIONS[min(step, len(POSITIONS) - 1)],
                    "confidence": 4, "after_turn_idx": idx, "ms_elapsed": 3100,
                })
            else:
                break
        await c.post("/debate/finish")

    conn = connect()
    try:
        d = conn.execute(
            "SELECT * FROM debates WHERE participant_id="
            "(SELECT id FROM participants WHERE code='DRYRUN-001')"
        ).fetchone()
        turns = conn.execute(
            "SELECT speaker, constraint_cell, substr(content,1,60) AS c FROM turns "
            "WHERE debate_id=? ORDER BY idx", (d["id"],)
        ).fetchall()
        ratings = conn.execute(
            "SELECT after_turn_idx, position, confidence, ms_elapsed FROM ratings "
            "WHERE debate_id=? ORDER BY after_turn_idx", (d["id"],)
        ).fetchall()
        calls = conn.execute(
            "SELECT purpose, COUNT(*) n FROM model_calls WHERE debate_id=? "
            "GROUP BY purpose", (d["id"],)
        ).fetchall()
        plan = conn.execute("SELECT plan_json FROM debates WHERE id=?", (d["id"],)).fetchone()[0]
        prof = conn.execute(
            "SELECT frozen_at, rendered FROM profiles WHERE participant_id="
            "(SELECT id FROM participants WHERE code='DRYRUN-001')"
        ).fetchone()
    finally:
        conn.close()

    print(f"debate id={d['id']} status={d['status']} frozen={d['frozen_at']}")
    print(f"plan: {plan}")
    print(f"profile frozen at {prof['frozen_at']} (before any turn: "
          f"{prof['frozen_at'] <= turns[0][0] if turns else 'n/a'})")
    print(f"\n{len(turns)} turns:")
    for i, t in enumerate(turns):
        cell = f" [{t['constraint_cell']}]" if t["constraint_cell"] else ""
        print(f"  {i:>2} {t['speaker']:<13}{cell:<18} {t['c']}...")
    print(f"\n{len(ratings)} ratings:")
    for r in ratings:
        print(f"  after turn {r['after_turn_idx']:>2}: pos={r['position']} "
              f"conf={r['confidence']} ({r['ms_elapsed']}ms)")
    print("\nmodel calls: " + ", ".join(f"{c['purpose']}={c['n']}" for c in calls))

    ok = (
        d["status"] == "frozen"
        and len([t for t in turns if t["speaker"] == "principal"]) == 8
        and len([t for t in turns if t["speaker"] == "interlocutor"]) == 8
        and len(ratings) == 9
        and prof["frozen_at"] is not None
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use real OpenRouter calls")
    ap.add_argument("--keep", action="store_true", help="write to the real db")
    args = ap.parse_args()
    if not args.keep:
        tmp = Path(tempfile.mkdtemp()) / "dryrun.db"
        os.environ["DELEGATES_DB"] = str(tmp)
        os.environ["DELEGATES_JOURNAL_MODE"] = "truncate"
    sys.exit(asyncio.run(run(args.live)))
