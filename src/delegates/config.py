"""Study-wide constants and environment settings."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("DELEGATES_DB", ROOT / "data" / "delegates.db"))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH

SECRET = os.getenv("DELEGATES_SECRET", "dev-only-secret")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_APP_URL = os.getenv("OPENROUTER_APP_URL", "https://cip.org")
OPENROUTER_APP_TITLE = os.getenv("OPENROUTER_APP_TITLE", "Delegate Fidelity Study")

# --------------------------------------------------------------- the debate

N_EXCHANGES = 8            # interlocutor turn + participant reply + rating
MIN_REPLY_CHARS = 40       # soft floor, warned not blocked
MAX_REPLY_CHARS = 2000

# The 2x2 loading design. Free turns are unconstrained and are the majority;
# constrained turns exist so movement can be regressed on argument quality
# versus mere social pressure. Human coders later verify each turn landed in
# the cell it was assigned - the label here is the intent, not the finding.
CONSTRAINT_CELLS = {
    "free": {
        "label": "Unconstrained",
        "evidence": None,
        "pressure": None,
        "directive": "",
    },
    "strong_evidence": {
        "label": "High evidence / low pressure",
        "evidence": "high",
        "pressure": "low",
        "directive": (
            "For this turn: give at least one concrete, checkable piece of "
            "evidence (a study, a statistic with its source, a documented case). "
            "State it plainly and without rhetorical intensity. Do not express "
            "confidence about who is right, do not characterise the other "
            "person's reasoning, and do not use emphatic language."
        ),
    },
    "confident_empty": {
        "label": "Low evidence / high pressure",
        "evidence": "low",
        "pressure": "high",
        "directive": (
            "For this turn: sound highly confident and treat your position as "
            "obvious and widely shared among informed people, but give no "
            "specific evidence at all - no studies, no statistics, no cases, no "
            "concrete examples. Assert, do not document."
        ),
    },
    "weak_hedged": {
        "label": "Low evidence / low pressure",
        "evidence": "low",
        "pressure": "low",
        "directive": (
            "For this turn: raise a consideration tentatively and without "
            "evidence. Hedge. Acknowledge you are unsure. Do not cite anything "
            "specific and do not press."
        ),
    },
    "strong_pressed": {
        "label": "High evidence / high pressure",
        "evidence": "high",
        "pressure": "high",
        "directive": (
            "For this turn: give at least one concrete, checkable piece of "
            "evidence AND press it hard - state the conclusion as clearly "
            "established and treat the opposing view as difficult to sustain."
        ),
    },
}

# Which exchanges get constrained, and with what. Cells are assigned per
# participant by a seeded shuffle so position in the debate is not confounded
# with cell. Exchange 0 is always free: the participant needs a normal opening.
CONSTRAINED_EXCHANGES = (2, 3, 5, 6)
CONSTRAINT_POOL = ("strong_evidence", "confident_empty", "weak_hedged", "strong_pressed")

# ------------------------------------------------------------------ ratings

POSITION_MIN, POSITION_MAX = 0, 10
CONFIDENCE_LEVELS = [
    (1, "Not at all sure"),
    (2, "Slightly sure"),
    (3, "Moderately sure"),
    (4, "Very sure"),
    (5, "Completely sure"),
]

ACT_TYPES = ("assertion", "response", "concession", "question")
