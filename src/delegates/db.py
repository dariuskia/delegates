"""SQLite access. One connection per request/task; WAL so reads never block."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    _set_journal_mode(conn)
    return conn


def _set_journal_mode(conn: sqlite3.Connection) -> str:
    """WAL where the filesystem supports it, TRUNCATE where it does not.

    Network and FUSE-backed mounts (including the folder this repo may live in
    during development) cannot do the shared-memory mapping WAL needs, and some
    cannot unlink a rollback journal either. TRUNCATE works on both and is
    adequate at study scale; production hosts get WAL automatically.
    """
    preferred = os.getenv("DELEGATES_JOURNAL_MODE", "wal").lower()
    for mode in (preferred, "truncate"):
        try:
            got = conn.execute(f"PRAGMA journal_mode = {mode}").fetchone()[0]
            conn.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
            conn.execute("DROP TABLE IF EXISTS _probe")
            return got
        except sqlite3.OperationalError:
            continue
    raise sqlite3.OperationalError("no usable SQLite journal mode for this filesystem")


@contextmanager
def session(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(path: Path | str | None = None) -> Path:
    p = Path(path or DB_PATH)
    conn = connect(p)
    try:
        conn.executescript(SCHEMA.read_text())
    finally:
        conn.close()
    return p


# ------------------------------------------------------------------ helpers

def insert(conn: sqlite3.Connection, table: str, **values: Any) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
    )
    return int(cur.lastrowid)


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def unjs(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
