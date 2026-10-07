"""Job creation and processing logic (no Flask in here)."""
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import db
from .certificate import render_certificate
from .validation import validate_recipient

log = logging.getLogger(__name__)

# Job statuses
PENDING, PROCESSING = "pending", "processing"
COMPLETED, COMPLETED_WITH_ERRORS, FAILED = "completed", "completed_with_errors", "failed"
# Certificate statuses
C_PENDING, C_SUCCESS, C_FAILED = "pending", "success", "failed"


@dataclass
class Settings:
    db_path: str
    storage_dir: str
    run_async: bool = True          # False => process inside the request (used by tests)
    max_workers: int = 2
    recover_on_start: bool = True   # resume unfinished jobs after a restart
    renderer: Callable = render_certificate   # injectable so tests can simulate failures
    executor: ThreadPoolExecutor = field(default=None, repr=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid():
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- create

def create_job(settings, job_data, raw_recipients):
    """Persist a job and one certificate row per recipient. Returns the job id.

    Recipients that fail validation are stored as 'failed' immediately (with the
    reason); valid ones are 'pending' and get picked up by the worker.
    """
    job_id, now = _uid(), _now()
    rows, pending = [], 0
    for position, raw in enumerate(raw_recipients):
        clean, error = validate_recipient(raw)
        status = C_FAILED if error else C_PENDING
        pending += status == C_PENDING
        rows.append((_uid(), job_id, position, clean["name"], clean["email"],
                     status, error, None, now, now))

    job_status = PENDING if pending else FAILED   # nothing valid => nothing to process
    with db.connection(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, event_name, issued_by, issue_date, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (job_id, job_data["event_name"], job_data["issued_by"], job_data["issue_date"],
             job_status, now, now))
        conn.executemany(
            "INSERT INTO certificates (id, job_id, position, recipient_name, recipient_email,"
            " status, error, file_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return job_id, bool(pending)


def dispatch(settings, job_id):
    """Run the job now (sync mode) or hand it to the background pool."""
    if settings.run_async:
        settings.executor.submit(_safe_process, settings, job_id)
    else:
        process_job(settings, job_id)


# ------------------------------------------------------------------------- process

def _safe_process(settings, job_id):
    # A thread-pool swallows exceptions silently; log them and mark the job failed.
    try:
        process_job(settings, job_id)
    except Exception:
        log.exception("job %s crashed", job_id)
        _set_job_status(settings, job_id, FAILED)


def _set_job_status(settings, job_id, status):
    with db.connection(settings.db_path) as conn:
        conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, _now(), job_id))


def process_job(settings, job_id):
    """Generate every pending certificate of a job, one at a time.

    Each certificate is its own transaction and its own try/except, so a failure
    on one never rolls back or blocks the others.
    """
    with db.connection(settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return
        todo = conn.execute(
            "SELECT id, recipient_name FROM certificates WHERE job_id=? AND status=? ORDER BY position",
            (job_id, C_PENDING)).fetchall()
    _set_job_status(settings, job_id, PROCESSING)

    out_dir = Path(settings.storage_dir) / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    for cert in todo:
        final_path = out_dir / f"{cert['id']}.pdf"
        tmp_path = out_dir / f"{cert['id']}.pdf.tmp"
        try:
            settings.renderer(tmp_path, recipient_name=cert["recipient_name"],
                              event_name=job["event_name"], issue_date=job["issue_date"],
                              issued_by=job["issued_by"])
            os.replace(tmp_path, final_path)    # atomic: never expose a half-written file
            _mark(settings, cert["id"], C_SUCCESS, file_path=str(final_path))
        except Exception as exc:
            log.warning("certificate %s failed: %s", cert["id"], exc)
            tmp_path.unlink(missing_ok=True)
            _mark(settings, cert["id"], C_FAILED, error=f"generation failed: {exc}"[:300])

    _set_job_status(settings, job_id, final_status(counts(settings, job_id)))


def _mark(settings, cert_id, status, file_path=None, error=None):
    with db.connection(settings.db_path) as conn:
        conn.execute("UPDATE certificates SET status=?, file_path=?, error=?, updated_at=? WHERE id=?",
                     (status, file_path, error, _now(), cert_id))


# --------------------------------------------------------------------------- query

def counts(settings, job_id):
    with db.connection(settings.db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM certificates WHERE job_id=? GROUP BY status",
                            (job_id,)).fetchall()
    c = {r["status"]: r["n"] for r in rows}
    return {"total": sum(c.values()), "succeeded": c.get(C_SUCCESS, 0),
            "failed": c.get(C_FAILED, 0), "pending": c.get(C_PENDING, 0)}


def final_status(c):
    if c["failed"] == 0:
        return COMPLETED
    return FAILED if c["succeeded"] == 0 else COMPLETED_WITH_ERRORS


def job_summary(settings, job_id):
    """Job row + progress counters, or None if the job doesn't exist."""
    with db.connection(settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        return None
    c = counts(settings, job_id)
    processed = c["succeeded"] + c["failed"]
    return {
        "id": job["id"], "status": job["status"], "event_name": job["event_name"],
        "issued_by": job["issued_by"], "issue_date": job["issue_date"],
        "created_at": job["created_at"], "updated_at": job["updated_at"],
        "progress": {**c, "processed": processed,
                     "percent": round(100 * processed / c["total"], 1) if c["total"] else 100.0},
    }


def list_certificates(settings, job_id, status=None, limit=100, offset=0):
    sql = "SELECT * FROM certificates WHERE job_id=?"
    args = [job_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY position LIMIT ? OFFSET ?"
    args += [limit, offset]
    with db.connection(settings.db_path) as conn:
        return conn.execute(sql, args).fetchall()


def get_certificate(settings, cert_id):
    with db.connection(settings.db_path) as conn:
        return conn.execute("SELECT * FROM certificates WHERE id=?", (cert_id,)).fetchone()


def unfinished_job_ids(settings):
    with db.connection(settings.db_path) as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE status IN (?,?)", (PENDING, PROCESSING))]
