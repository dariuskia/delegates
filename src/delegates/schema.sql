-- AI Delegate Fidelity study - core schema.
-- Design principle: a "debate" is any run through the debate environment.
-- Principal live sessions, delegate free-runs and teacher-forced replays are
-- all rows in `debates`, distinguished by (actor, mode). That is what makes
-- moment-by-moment comparison a join rather than a special case.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- topics

CREATE TABLE IF NOT EXISTS topics (
    id                  INTEGER PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    proposition         TEXT NOT NULL,   -- the 0-10 scale is agreement with this
    scale_low_label     TEXT NOT NULL DEFAULT 'Strongly oppose',
    scale_high_label    TEXT NOT NULL DEFAULT 'Strongly support',
    jurisdiction        TEXT,            -- residency screen, e.g. 'US-CA'
    background          TEXT,            -- neutral framing shown once
    model_lean          REAL,            -- from the generic-floor lean scan
    model_lean_strength REAL,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------- participants

CREATE TABLE IF NOT EXISTS participants (
    id                 INTEGER PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,   -- Prolific PID or pilot code
    cohort             TEXT NOT NULL DEFAULT 'pilot',
    debated_topic_id   INTEGER REFERENCES topics(id),
    cold_topic_id      INTEGER REFERENCES topics(id),
    consent_recontact  INTEGER NOT NULL DEFAULT 0,
    consent_release    INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'enrolled',
    notes              TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Session 1 / 2 / 3 attendance and resumable access tokens.
CREATE TABLE IF NOT EXISTS study_sessions (
    id             INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    session_no     INTEGER NOT NULL,          -- 1 profile+debate, 2 retest, 3 review
    token          TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL DEFAULT 'pending',
    started_at     TEXT,
    completed_at   TEXT,
    UNIQUE (participant_id, session_no)
);

-- ---------------------------------------------------------------- profiles
-- The firewall lives here: profiles are written in session 1 BEFORE the debate
-- and frozen. `rendered` is the exact text a delegate is conditioned on, so a
-- released profile can rebuild the delegate by any method.

CREATE TABLE IF NOT EXISTS profiles (
    id             INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    kind           TEXT NOT NULL,   -- full | survey_only | lookalike | none
    format_variant TEXT NOT NULL DEFAULT 'default',
    payload_json   TEXT NOT NULL,   -- structured survey + interview + dispositional
    rendered       TEXT,            -- what actually goes in the prompt
    source_ids     TEXT,            -- for lookalike: JSON list of donor participant ids
    frozen_at      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ----------------------------------------------------------- agent configs
-- "Agent classes with varying access to context" are rows here, not subclasses.
-- Adding an arm to the sensitivity band is an INSERT.

CREATE TABLE IF NOT EXISTS agent_configs (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    role           TEXT NOT NULL,   -- interlocutor | delegate
    model          TEXT NOT NULL,   -- OpenRouter model id, pinned
    params_json    TEXT NOT NULL DEFAULT '{}',
    prompt_variant TEXT NOT NULL DEFAULT 'neutral',
    context_policy TEXT NOT NULL DEFAULT 'full',   -- full | opponent_only | none
    profile_kind   TEXT NOT NULL DEFAULT 'full',   -- full | survey_only | lookalike | none
    rating_mode    TEXT NOT NULL DEFAULT 'separate_call',
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- debates

CREATE TABLE IF NOT EXISTS debates (
    id                   INTEGER PRIMARY KEY,
    participant_id       INTEGER NOT NULL REFERENCES participants(id),
    topic_id             INTEGER NOT NULL REFERENCES topics(id),
    actor                TEXT NOT NULL,  -- principal | delegate
    mode                 TEXT NOT NULL,  -- live | free_run | replay_full | replay_opponent_only
    source_debate_id     INTEGER REFERENCES debates(id),  -- replay: the frozen principal run
    interlocutor_cfg_id  INTEGER REFERENCES agent_configs(id),
    delegate_cfg_id      INTEGER REFERENCES agent_configs(id),
    profile_id           INTEGER REFERENCES profiles(id),
    plan_json            TEXT NOT NULL DEFAULT '{}',  -- constraint-cell schedule, n_exchanges
    seed                 INTEGER,
    run_index            INTEGER NOT NULL DEFAULT 0,   -- for the noise floor
    status               TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress|frozen|abandoned
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    frozen_at            TEXT
);
CREATE INDEX IF NOT EXISTS ix_debates_participant ON debates(participant_id, topic_id);
CREATE INDEX IF NOT EXISTS ix_debates_source ON debates(source_debate_id);

-- ------------------------------------------------------------------ turns
-- idx counts every utterance in order. `source_turn_id` is the replay join:
-- a delegate turn points at the principal turn it was asked to stand in for.

CREATE TABLE IF NOT EXISTS turns (
    id             INTEGER PRIMARY KEY,
    debate_id      INTEGER NOT NULL REFERENCES debates(id),
    idx            INTEGER NOT NULL,
    speaker        TEXT NOT NULL,   -- interlocutor | principal | delegate
    content        TEXT NOT NULL,
    act_type       TEXT,            -- assertion | response | concession | question
    constraint_cell TEXT,           -- interlocutor turns only; see config.CONSTRAINT_CELLS
    source_turn_id INTEGER REFERENCES turns(id),
    model_call_id  INTEGER,
    ms_elapsed     INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (debate_id, idx)
);
CREATE INDEX IF NOT EXISTS ix_turns_source ON turns(source_turn_id);

-- ---------------------------------------------------------------- ratings
-- `source` keeps human slider taps and model-emitted numbers distinguishable,
-- so a rating-elicitation artifact can never masquerade as movement fidelity.

CREATE TABLE IF NOT EXISTS ratings (
    id             INTEGER PRIMARY KEY,
    debate_id      INTEGER NOT NULL REFERENCES debates(id),
    after_turn_idx INTEGER NOT NULL,
    position       REAL NOT NULL,      -- 0-10 agreement with topic.proposition
    confidence     INTEGER,            -- 1-5
    source         TEXT NOT NULL DEFAULT 'participant',
                                       -- participant | agent_reported | agent_extracted
    ms_elapsed     INTEGER,
    raw            TEXT,               -- agent: the text the number came from
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (debate_id, after_turn_idx, source)
);

-- ------------------------------------------------------------ model calls
-- Every provider call, logged. Cost tracking, reproducibility, and the
-- prompt-sensitivity band all read from here.

CREATE TABLE IF NOT EXISTS model_calls (
    id                INTEGER PRIMARY KEY,
    debate_id         INTEGER REFERENCES debates(id),
    agent_config_id   INTEGER REFERENCES agent_configs(id),
    purpose           TEXT NOT NULL,   -- turn | rating | lean_scan | profile_render
    model             TEXT NOT NULL,
    request_json      TEXT NOT NULL,
    response_json     TEXT,
    content           TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          REAL,
    latency_ms        INTEGER,
    seed              INTEGER,
    cache_key         TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_model_calls_cache ON model_calls(cache_key);

-- ------------------------------------- session 2: self-consistency ceiling

CREATE TABLE IF NOT EXISTS retest_items (
    id             INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    source_turn_id INTEGER NOT NULL REFERENCES turns(id),  -- the interlocutor argument
    kind           TEXT NOT NULL DEFAULT 'retest',   -- retest | filler
    order_idx      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS retest_responses (
    id             INTEGER PRIMARY KEY,
    retest_item_id INTEGER NOT NULL REFERENCES retest_items(id),
    position       REAL NOT NULL,
    confidence     INTEGER,
    ms_elapsed     INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recall_probes (
    id             INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    source_turn_id INTEGER REFERENCES turns(id),
    recalled       INTEGER,          -- 0/1 claimed recall
    confidence     INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------- session 3: authorization

CREATE TABLE IF NOT EXISTS review_items (
    id             INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    turn_id        INTEGER NOT NULL REFERENCES turns(id),   -- the delegate act judged
    kind           TEXT NOT NULL,  -- own | lookalike_blind | repeat | generic_blind
    order_idx      INTEGER NOT NULL,
    shown_position_delta REAL      -- explicit position change displayed, if any
);

CREATE TABLE IF NOT EXISTS authorizations (
    id             INTEGER PRIMARY KEY,
    review_item_id INTEGER NOT NULL REFERENCES review_items(id),
    verdict        TEXT NOT NULL,   -- yes | reservations | no
    comment        TEXT,
    ms_elapsed     INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------- human coding of interlocutor

CREATE TABLE IF NOT EXISTS argument_codes (
    id              INTEGER PRIMARY KEY,
    turn_id         INTEGER NOT NULL REFERENCES turns(id),
    coder_code      TEXT NOT NULL,
    evidence_score  INTEGER,   -- argument quality
    pressure_score  INTEGER,   -- social/confidence pressure
    cell_guess      TEXT,      -- blind guess at the constraint cell
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (turn_id, coder_code)
);

-- --------------------------------------------------------------- events
-- Append-only audit of participant actions; useful for timing and dropout.

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    participant_id INTEGER REFERENCES participants(id),
    debate_id      INTEGER REFERENCES debates(id),
    kind           TEXT NOT NULL,
    payload_json   TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
