"""Participant-facing app (Sessions 1-3; Session 1 is implemented).

Design notes that matter:

* The server owns the state machine. Every POST re-derives the stage from the
  database and returns the whole inner view, so a refresh, a back button or a
  dropped connection can never desynchronise a participant's transcript.
* The profile is written and frozen BEFORE the debate route will open. The
  firewall is enforced here, not by discipline.
* Rating timing is recorded (ms from stage render to submit) because a rating
  tapped in 400ms is different evidence from one tapped in nine seconds.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import study
from ..agents.base import Turn
from ..config import CONFIDENCE_LEVELS, MAX_REPLY_CHARS, MIN_REPLY_CHARS, SECRET
from ..db import all_rows, connect, init_db, insert, js, one, unjs

HERE = Path(__file__).resolve().parent

init_db()

app = FastAPI(title="Delegate Fidelity Study")
app.add_middleware(SessionMiddleware, secret_key=SECRET, https_only=False)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def render(name: str, ctx: dict) -> HTMLResponse:
    """Starlette wants the request positionally; keep it in the context dict at
    the call sites so fragment and page renders share one shape."""
    request = ctx.pop("request")
    return templates.TemplateResponse(request, name, ctx)


def db():
    return connect()


def current_participant(request: Request):
    pid = request.session.get("pid")
    if not pid:
        return None
    conn = db()
    try:
        return one(conn, "SELECT * FROM participants WHERE id = ?", (pid,))
    finally:
        conn.close()


def require_participant(request: Request):
    p = current_participant(request)
    if not p:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return p


# ------------------------------------------------------------- entry points

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render("index.html", {"request": request})


@app.post("/start")
def start(request: Request, code: str = Form(...), cohort: str = Form("pilot")):
    conn = db()
    try:
        conn.execute("BEGIN")
        participant = study.enroll(conn, code.strip(), cohort)
        token = study.session_token(conn, participant["id"], 1)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return RedirectResponse(f"/s/{token}", status_code=303)


@app.get("/s/{token}")
def resume(request: Request, token: str):
    conn = db()
    try:
        found = study.participant_for_token(conn, token)
        if not found:
            raise HTTPException(404, "Unknown study link")
        participant, session_no = found
        conn.execute(
            "UPDATE study_sessions SET status='in_progress', "
            "started_at=COALESCE(started_at, datetime('now')) WHERE token=?",
            (token,),
        )
    finally:
        conn.close()
    request.session["pid"] = participant["id"]
    request.session["session_no"] = session_no
    if session_no == 1:
        return RedirectResponse("/consent", status_code=303)
    if session_no == 3:
        return RedirectResponse("/authorize", status_code=303)
    return RedirectResponse("/not-yet", status_code=303)


@app.get("/not-yet", response_class=HTMLResponse)
def not_yet(request: Request):
    return render("not_yet.html", {"request": request})


# ------------------------------------------------------------------ consent

@app.get("/consent", response_class=HTMLResponse)
def consent_form(request: Request):
    participant = require_participant(request)
    return render(
        "consent.html", {"request": request, "participant": participant}
    )


@app.post("/consent")
def consent_submit(
    request: Request,
    agree: str = Form(...),
    recontact: str = Form("off"),
    release: str = Form("off"),
):
    participant = require_participant(request)
    conn = db()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE participants SET consent_recontact=?, consent_release=?, "
            "status='consented' WHERE id=?",
            (1 if recontact == "on" else 0, 1 if release == "on" else 0,
             participant["id"]),
        )
        study.log_event(conn, "consent", participant_id=participant["id"])
        conn.execute("COMMIT")
    finally:
        conn.close()
    return RedirectResponse("/profile", status_code=303)


# ------------------------------------------------------------------ profile
# Placeholder instrument. The full survey + adaptive interview + dispositional
# module replaces the body of this form; the ordering guarantee it provides -
# profile frozen before any debate turn exists - is already real.

PROFILE_FIELDS = [
    ("age_band", "Age", ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"]),
    ("politics", "Where would you place yourself politically?",
     ["Left", "Centre-left", "Centre", "Centre-right", "Right", "None of these"]),
    ("openness", "In general, how open are you to changing your mind on political questions?",
     ["Not at all", "A little", "Somewhat", "Quite", "Very"]),
]


@app.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request):
    participant = require_participant(request)
    conn = db()
    try:
        existing = one(
            conn, "SELECT * FROM profiles WHERE participant_id=? AND frozen_at IS NOT NULL",
            (participant["id"],),
        )
    finally:
        conn.close()
    if existing:
        return RedirectResponse("/debate", status_code=303)
    return render(
        "profile.html",
        {"request": request, "participant": participant, "fields": PROFILE_FIELDS},
    )


@app.post("/profile")
async def profile_submit(request: Request):
    participant = require_participant(request)
    form = await request.form()
    payload = {k: v for k, v in form.items()}
    conn = db()
    try:
        conn.execute("BEGIN")
        rendered = "\n".join(
            f"- {label}: {payload.get(key, '(not given)')}"
            for key, label, _ in PROFILE_FIELDS
        )
        free = payload.get("mind_change", "").strip()
        if free:
            rendered += f"\n- A time they changed their mind: {free}"
        insert(
            conn, "profiles", participant_id=participant["id"], kind="full",
            payload_json=js(payload), rendered=rendered,
            frozen_at=None,
        )
        conn.execute(
            "UPDATE profiles SET frozen_at = datetime('now') "
            "WHERE participant_id = ? AND frozen_at IS NULL",
            (participant["id"],),
        )
        study.log_event(conn, "profile_frozen", participant_id=participant["id"])
        conn.execute("COMMIT")
    finally:
        conn.close()
    return RedirectResponse("/debate", status_code=303)


# ------------------------------------------------------------------- debate

def _debate_context(conn, request: Request, participant):
    debate = study.start_live_debate(conn, participant)
    topic = study.topic_of(conn, debate)
    turns = study.transcript(conn, debate["id"])
    plan = unjs(debate["plan_json"], {}) or {}
    return {
        "request": request,
        "participant": participant,
        "debate": debate,
        "topic": topic,
        "turns": [t for t in turns],
        "stage": study.stage(conn, debate),
        "exchange": study.current_exchange(conn, debate["id"]),
        "n_exchanges": plan.get("n_exchanges", 8),
        "confidence_levels": CONFIDENCE_LEVELS,
        "min_chars": MIN_REPLY_CHARS,
        "max_chars": MAX_REPLY_CHARS,
        "last_self_idx": max(
            (t.idx for t in turns if t.speaker in ("principal", "delegate")),
            default=study.OPENING_RATING_IDX,
        ),
    }


@app.get("/debate", response_class=HTMLResponse)
def debate_page(request: Request):
    participant = require_participant(request)
    conn = db()
    try:
        frozen = one(
            conn, "SELECT 1 FROM profiles WHERE participant_id=? AND frozen_at IS NOT NULL",
            (participant["id"],),
        )
        if not frozen:
            return RedirectResponse("/profile", status_code=303)
        conn.execute("BEGIN")
        ctx = _debate_context(conn, request, participant)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return render("debate.html", ctx)


@app.get("/debate/view", response_class=HTMLResponse)
def debate_view(request: Request):
    participant = require_participant(request)
    conn = db()
    try:
        conn.execute("BEGIN")
        ctx = _debate_context(conn, request, participant)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return render("_inner.html", ctx)


@app.post("/debate/rating", response_class=HTMLResponse)
def debate_rating(
    request: Request,
    position: float = Form(...),
    confidence: int = Form(...),
    after_turn_idx: int = Form(...),
    ms_elapsed: int = Form(0),
):
    participant = require_participant(request)
    conn = db()
    try:
        conn.execute("BEGIN")
        debate = study.start_live_debate(conn, participant)
        study.record_rating(
            conn, debate["id"], after_turn_idx, position, confidence, ms_elapsed
        )
        ctx = _debate_context(conn, request, participant)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return render("_inner.html", ctx)


@app.post("/debate/reply", response_class=HTMLResponse)
def debate_reply(
    request: Request, content: str = Form(...), ms_elapsed: int = Form(0)
):
    participant = require_participant(request)
    text = content.strip()[:MAX_REPLY_CHARS]
    conn = db()
    try:
        conn.execute("BEGIN")
        debate = study.start_live_debate(conn, participant)
        if text and study.stage(conn, debate) == "reply":
            study.append_turn(
                conn, debate["id"], "principal", text, ms_elapsed=ms_elapsed
            )
        ctx = _debate_context(conn, request, participant)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return render("_inner.html", ctx)


@app.post("/debate/advance", response_class=HTMLResponse)
async def debate_advance(request: Request):
    participant = require_participant(request)
    conn = db()
    try:
        debate = None
        conn.execute("BEGIN")
        debate = study.start_live_debate(conn, participant)
        conn.execute("COMMIT")
        if study.stage(conn, debate) == "await_interlocutor":
            conn.execute("BEGIN")
            await study.generate_interlocutor_turn(conn, debate)
            conn.execute("COMMIT")
        conn.execute("BEGIN")
        ctx = _debate_context(conn, request, participant)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return render("_inner.html", ctx)


@app.post("/debate/finish")
def debate_finish(request: Request):
    participant = require_participant(request)
    conn = db()
    try:
        conn.execute("BEGIN")
        debate = study.start_live_debate(conn, participant)
        study.freeze(conn, debate["id"])
        conn.execute(
            "UPDATE study_sessions SET status='complete', "
            "completed_at=datetime('now') WHERE participant_id=? AND session_no=1",
            (participant["id"],),
        )
        study.log_event(
            conn, "session1_complete", participant_id=participant["id"],
            debate_id=debate["id"],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return RedirectResponse("/done", status_code=303)


@app.get("/done", response_class=HTMLResponse)
def done(request: Request):
    return render("done.html", {"request": request})


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": time.time()}


# ---------------------------------------------------------------- authorize
# Session 3. Items are built on first entry and served one at a time; the
# server re-derives the next unanswered item on every request, as in Session 1.

def _authorize_context(conn, request: Request, participant):
    items = study.build_review_items(conn, participant)
    item = study.next_review_item(conn, participant["id"])
    done = one(conn, "SELECT COUNT(*) AS n FROM authorizations a JOIN review_items r "
                     "ON r.id = a.review_item_id WHERE r.participant_id = ?",
               (participant["id"],))["n"]
    ctx = {"request": request, "participant": participant, "n_items": len(items),
           "n_done": done, "item": None}
    if item:
        ctx.update(study.review_item_context(conn, item))
    return ctx


@app.get("/authorize", response_class=HTMLResponse)
def authorize_page(request: Request):
    participant = require_participant(request)
    conn = db()
    try:
        conn.execute("BEGIN")
        ctx = _authorize_context(conn, request, participant)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return render("authorize.html", ctx)


@app.post("/authorize/verdict", response_class=HTMLResponse)
def authorize_verdict(
    request: Request, review_item_id: int = Form(...), verdict: str = Form(...),
    comment: str = Form(""), ms_elapsed: int = Form(0),
):
    participant = require_participant(request)
    conn = db()
    try:
        conn.execute("BEGIN")
        owner = one(conn, "SELECT participant_id FROM review_items WHERE id = ?",
                    (review_item_id,))
        if owner and owner["participant_id"] == participant["id"]:
            study.record_authorization(conn, review_item_id, verdict, comment, ms_elapsed)
        ctx = _authorize_context(conn, request, participant)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return render("_authorize_inner.html", ctx)


@app.post("/authorize/finish")
def authorize_finish(request: Request):
    participant = require_participant(request)
    conn = db()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE study_sessions SET status='complete', completed_at=datetime('now') "
            "WHERE participant_id=? AND session_no=3", (participant["id"],),
        )
        study.log_event(conn, "session3_complete", participant_id=participant["id"])
        conn.execute("COMMIT")
    finally:
        conn.close()
    return RedirectResponse("/done", status_code=303)


# ------------------------------------------------------------------- review
# Researcher-facing, read-only. Lists every debate and shows a transcript; for
# a delegate replay each exchange is shown beside the principal turn it stood
# in for (the `source_turn_id` join) with both sides' ratings. No auth: this is
# a local research tool, do not expose it on a participant-facing host.


def _ratings_by_idx(conn, debate_id: int, source: str) -> dict[int, dict]:
    return {
        r["after_turn_idx"]: dict(r)
        for r in all_rows(
            conn, "SELECT * FROM ratings WHERE debate_id=? AND source=?",
            (debate_id, source),
        )
    }


@app.get("/review", response_class=HTMLResponse)
def review_index(request: Request):
    conn = db()
    try:
        rows = all_rows(conn, """
            SELECT d.*, p.code, t.title,
                   c.name AS cfg_name, c.profile_kind, c.context_policy, c.prompt_variant,
                   (SELECT COUNT(*) FROM turns WHERE debate_id=d.id) AS n_turns,
                   (SELECT COUNT(*) FROM ratings WHERE debate_id=d.id) AS n_ratings,
                   (SELECT ROUND(SUM(cost_usd), 3) FROM model_calls WHERE debate_id=d.id) AS cost
            FROM debates d
            JOIN participants p ON p.id = d.participant_id
            JOIN topics t ON t.id = d.topic_id
            LEFT JOIN agent_configs c ON c.id = d.delegate_cfg_id
            ORDER BY COALESCE(d.source_debate_id, d.id), d.actor DESC, d.id
        """)
    finally:
        conn.close()
    groups: dict[int, list] = {}
    for r in rows:
        groups.setdefault(r["source_debate_id"] or r["id"], []).append(r)
    return render("review_index.html", {"request": request, "groups": groups})


@app.get("/review/{debate_id}", response_class=HTMLResponse)
def review_debate(request: Request, debate_id: int):
    conn = db()
    try:
        debate = one(conn, "SELECT * FROM debates WHERE id=?", (debate_id,))
        if not debate:
            raise HTTPException(404, "No such debate")
        topic = study.topic_of(conn, debate)
        participant = one(conn, "SELECT * FROM participants WHERE id=?",
                          (debate["participant_id"],))
        cfg = one(conn, "SELECT * FROM agent_configs WHERE id=?",
                  (debate["delegate_cfg_id"],)) if debate["delegate_cfg_id"] else None
        profile = one(conn, "SELECT * FROM profiles WHERE id=?",
                      (debate["profile_id"],)) if debate["profile_id"] else None
        turns = study.transcript(conn, debate_id)
        own_ratings = _ratings_by_idx(
            conn, debate_id,
            "participant" if debate["actor"] == "principal" else "agent_reported",
        )
        source_turns: dict[int, Turn] = {}
        source_ratings: dict[int, dict] = {}
        if debate["source_debate_id"]:
            source_turns = {t.id: t for t in study.transcript(conn, debate["source_debate_id"])}
            source_ratings = _ratings_by_idx(conn, debate["source_debate_id"], "participant")
        siblings = all_rows(conn, """
            SELECT d.id, d.mode, d.run_index, c.name AS cfg_name
            FROM debates d LEFT JOIN agent_configs c ON c.id = d.delegate_cfg_id
            WHERE d.id = ? OR d.source_debate_id = ? OR d.id = ?
            ORDER BY d.actor DESC, d.id
        """, (debate["source_debate_id"] or debate_id, debate["source_debate_id"] or debate_id,
              debate["source_debate_id"] or debate_id))
    finally:
        conn.close()

    # Group into exchanges: one interlocutor turn followed by the reply.
    exchanges, cur = [], None
    for t in turns:
        if t.speaker == "interlocutor":
            cur = {"opp": t, "own": None, "src": None}
            exchanges.append(cur)
        elif cur is not None:
            cur["own"] = t
            cur["src"] = source_turns.get(t.source_turn_id)
    return render("review_debate.html", {
        "request": request, "debate": debate, "topic": topic, "participant": participant,
        "cfg": cfg, "profile": profile, "exchanges": exchanges,
        "own_ratings": own_ratings, "source_ratings": source_ratings,
        "opening": own_ratings.get(study.OPENING_RATING_IDX),
        "source_opening": source_ratings.get(study.OPENING_RATING_IDX),
        "plan": unjs(debate["plan_json"], {}) or {}, "siblings": siblings,
        "is_replay": bool(debate["source_debate_id"]),
    })


@app.get("/review/authorizations/{participant_id}", response_class=HTMLResponse)
def review_authorizations(request: Request, participant_id: int):
    conn = db()
    try:
        participant = one(conn, "SELECT * FROM participants WHERE id=?", (participant_id,))
        if not participant:
            raise HTTPException(404, "No such participant")
        rows = all_rows(conn, """
            SELECT r.order_idx, r.kind, r.shown_position_delta, r.turn_id,
                   a.verdict, a.comment, a.ms_elapsed,
                   t.debate_id, t.idx AS turn_idx, substr(t.content, 1, 160) AS excerpt,
                   c.name AS cfg_name
            FROM review_items r
            LEFT JOIN authorizations a ON a.review_item_id = r.id
            JOIN turns t ON t.id = r.turn_id
            JOIN debates d ON d.id = t.debate_id
            LEFT JOIN agent_configs c ON c.id = d.delegate_cfg_id
            WHERE r.participant_id = ? ORDER BY r.order_idx
        """, (participant_id,))
        summary = all_rows(conn, """
            SELECT r.kind, a.verdict, COUNT(*) AS n
            FROM review_items r JOIN authorizations a ON a.review_item_id = r.id
            WHERE r.participant_id = ? GROUP BY r.kind, a.verdict
        """, (participant_id,))
        repeats = all_rows(conn, """
            SELECT r.turn_id, GROUP_CONCAT(a.verdict, ' / ') AS verdicts
            FROM review_items r JOIN authorizations a ON a.review_item_id = r.id
            WHERE r.participant_id = ? AND r.kind IN ('own', 'repeat')
            GROUP BY r.turn_id HAVING COUNT(*) > 1
        """, (participant_id,))
    finally:
        conn.close()
    table: dict[str, dict[str, int]] = {}
    for s in summary:
        table.setdefault(s["kind"], {})[s["verdict"]] = s["n"]
    return render("review_authorizations.html", {
        "request": request, "participant": participant, "rows": rows,
        "table": table, "repeats": repeats,
    })
