"""SQLite access layer (stdlib sqlite3).

Every call opens a short-lived connection. That keeps things thread-safe, since
the web threads and the background worker threads never share a connection.
WAL mode lets readers (status polling) proceed while a worker is writing.
"""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    event_name  TEXT NOT NULL,
    issued_by   TEXT,
    issue_date  TEXT NOT NULL,
    status      TEXT NOT NULL,            -- pending | processing | completed | completed_with_errors | failed
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    id               TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL REFERENCES jobs(id),
    position         INTEGER NOT NULL,    -- index of the recipient in the original request
    recipient_name   TEXT,
    recipient_email  TEXT,
    status           TEXT NOT NULL,       -- pending | success | failed
    error            TEXT,
    file_path        TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cert_job_status ON certificates(job_id, status);
CREATE INDEX IF NOT EXISTS idx_cert_job_pos ON certificates(job_id, position);
"""


def _connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connection(path):
    """Yield a connection; commit on success, roll back on error, always close."""
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path):
    with connection(path) as conn:
        conn.executescript(SCHEMA)
