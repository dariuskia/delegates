# Delegate Fidelity — study infrastructure

Instrument for *Can an AI Delegate Faithfully Stand In for You?* (design v3).
Three components: the participant-facing debate environment, a frozen record
that delegates can be teacher-forced on, and agent classes that vary in what
context they are allowed to see.

## Run it

```bash
cp .env.example .env          # add your OPENROUTER_API_KEY
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/delegates"
uv sync --extra dev
uv run python scripts/seed.py     # create the db, seed pilot topics
./scripts/serve.sh                # http://localhost:8000
```

Check the whole of Session 1 without spending an API call or a participant:

```bash
uv run python scripts/dry_run.py          # synthetic principal, stubbed model
uv run python scripts/dry_run.py --live   # same, against OpenRouter
uv run pytest -q tests/
```

The virtualenv is deliberately kept outside the repo — this folder is a mounted
directory on some machines and an in-tree `.venv` cannot be rewritten there.

## What is here

```
src/delegates/
  schema.sql        the whole study's data model (sessions 1-3 + human coding)
  db.py             connections; journal-mode fallback for mounted filesystems
  config.py         debate length, the 2x2 constraint cells, rating scales
  study.py          enrolment, debate state machine, persistence
  llm/openrouter.py client; every call logged to model_calls
  llm/registry.py   pinned models per role
  agents/base.py    Agent, Scene, Turn, context policies, rating elicitation
  agents/interlocutor.py  the arguer, constraint schedule, stance assignment
  agents/delegate.py      the delegate and its arms
  prompts/          system prompts, one file per variant
  web/              FastAPI app, templates, ~50 lines of vanilla JS
```

### Three decisions worth knowing about

**A debate is any run through the environment.** Principal live sessions,
delegate free-runs and teacher-forced replays are all rows in `debates`,
separated by `(actor, mode)`, with `turns.source_turn_id` pointing a delegate
turn at the principal turn it stood in for. Moment-by-moment comparison is
therefore a join, not a special case, and the replay harness adds no new
storage shape.

**Agent classes are rows, not subclasses.** What the study varies — model,
prompt variant, profile (full / survey-only / look-alike / none), and context
policy (`full` / `opponent_only` / `none`) — are fields on `agent_configs`.
Adding an arm to the sensitivity band is an INSERT. `agents/delegate.py`
lists the standard arms.

**Ratings are elicited in a separate call.** A delegate produces its turn, then
a second call with no rating history in context returns the position. Asking
for the number alongside the argument would let the model anchor on the
principal's own prior ratings, which are visible in full replay but not in
opponent-only — manufacturing the context-condition contrast instead of
measuring it. `ratings.source` keeps human taps and model numbers apart in the
data.

## Session 1 as a participant sees it

Consent → profile (frozen; the debate route will not open until it is) →
opening position rating → eight exchanges, each an interlocutor turn, a written
reply, and a two-tap position-and-confidence rating → freeze.

The server re-derives the stage from the database on every request, so a
refresh, a back button or a dropped connection cannot desynchronise a
transcript.

Constraint cells are assigned by a per-participant seeded shuffle over fixed
exchange slots, so pressure type is not confounded with position in the debate.
Cells are the *intent*; what a turn actually was is settled later by human
coders in `argument_codes`.

## Not built yet

- Replay harness (`replay_full` / `replay_opponent_only` runners) and scoring
- Delegate free-run driver for the cold topic
- Session 2 (self-consistency retest, recall probe)
- Session 3 (authorization review, blind discrimination items)
- Model-lean scan and the real topic set
- Look-alike profile construction
- The full profile instrument — the current form is a placeholder with the
  right ordering guarantee
- Export to the release format
