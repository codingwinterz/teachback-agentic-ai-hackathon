import json
import sqlite3

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    reference_notes TEXT NOT NULL,
    prerequisites_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    raw_transcript TEXT NOT NULL,
    clean_claims_json TEXT NOT NULL,
    hedging_score REAL NOT NULL,
    correct_json TEXT NOT NULL,
    missing_json TEXT NOT NULL,
    misconceptions_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS learner_state (
    concept_id TEXT PRIMARY KEY REFERENCES concepts(id),
    mastery REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    interval_days REAL NOT NULL DEFAULT 1,
    ease REAL NOT NULL DEFAULT 2.5,
    next_review_at TEXT,
    attempts_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS misconceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    label TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved INTEGER NOT NULL DEFAULT 0,
    UNIQUE (concept_id, label)
);

-- Mini-lessons from the teach step and the student's answer to the check question.
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    misconception_label TEXT,
    lesson_json TEXT NOT NULL,
    answer TEXT,
    correct INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Study sessions: the agent picks concepts with tools, up to 5 questions per session.
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    call_count INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS session_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    step_no INTEGER NOT NULL,
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    question TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempt_id INTEGER REFERENCES attempts(id),
    mastery_after REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- run_id, model, elapsed_s are additions to the CLAUDE.md schema.
CREATE TABLE IF NOT EXISTS agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    model TEXT,
    elapsed_s REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after first release: (table, column, type). Applied if missing.
MIGRATIONS = [
    ("agent_log", "model", "TEXT"),
    ("agent_log", "elapsed_s", "REAL"),
    ("learner_state", "is_sample", "INTEGER NOT NULL DEFAULT 0"),
    ("misconceptions", "is_sample", "INTEGER NOT NULL DEFAULT 0"),
    ("attempts", "is_sample", "INTEGER NOT NULL DEFAULT 0"),
]


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, col_type in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    conn.commit()


def seed_concepts(conn: sqlite3.Connection, path: str | None = None) -> int:
    with open(path or config.CONCEPTS_PATH, encoding="utf-8") as f:
        concepts = json.load(f)
    for c in concepts:
        conn.execute(
            """INSERT INTO concepts (id, name, subject, reference_notes, prerequisites_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, subject=excluded.subject,
                 reference_notes=excluded.reference_notes,
                 prerequisites_json=excluded.prerequisites_json""",
            (c["id"], c["name"], c["subject"], c["reference_notes"],
             json.dumps(c["prerequisites"])),
        )
        conn.execute(
            "INSERT OR IGNORE INTO learner_state (concept_id) VALUES (?)", (c["id"],)
        )
    conn.commit()
    return len(concepts)
