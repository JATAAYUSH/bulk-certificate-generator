# Bulk Certificate Generator

A small web interface and REST API that accept **one request containing many recipients**, generate a PDF certificate for each
from a single predefined template, tracks progress, and lets you retrieve the results (one by one or as a ZIP).

**Stack:** Python 3.10+, Flask, SQLite (relational DB, standard-library `sqlite3`), ReportLab (PDF).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python run.py                      # http://127.0.0.1:5000
```

Open `http://127.0.0.1:5000/` for the browser interface. Enter one recipient per line; optionally paste a tab-separated email after each name. The page shows generation progress and provides individual PDF links and a ZIP download.

The SQLite database and generated PDFs are stored in `./data/` (created automatically).

## Test

```bash
python -m unittest discover -v     # standard library only; `pytest` also works if you have it
```

## API

### 1. Submit a generation request – `POST /api/jobs`

```bash
curl -X POST http://127.0.0.1:5000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
        "event_name": "Python Bootcamp 2025",
        "issued_by": "Acme Academy",
        "issue_date": "2025-03-14",
        "recipients": [
          {"name": "Asha Patel", "email": "asha@example.com"},
          {"name": "Ravi Shah"},
          {"name": "", "email": "broken"}
        ]
      }'
```

| Field | Required | Notes |
|---|---|---|
| `event_name` | yes | max 150 chars |
| `recipients` | yes | non-empty list, max 1000 per request |
| `recipients[].name` | yes | max 100 chars |
| `recipients[].email` | no | validated if present (stored only; no email is sent) |
| `issued_by` | no | printed on the certificate |
| `issue_date` | no | ISO date, defaults to today |

Response – `202 Accepted` (job queued/running; `201` if it already finished):

```json
{
  "id": "473a60a6...",
  "status": "pending",
  "progress": {"total": 3, "succeeded": 0, "failed": 1, "pending": 2, "processed": 1, "percent": 33.3},
  "links": {"self": "/api/jobs/473a...", "certificates": "/api/jobs/473a.../certificates",
            "download_all": "/api/jobs/473a.../download"}
}
```

### 2. Check progress – `GET /api/jobs/{id}`

Poll until `status` is `completed`, `completed_with_errors` or `failed`.

Job statuses: `pending` → `processing` → `completed` (all ok) / `completed_with_errors` (some failed) / `failed` (none succeeded).

### 3. See per-recipient results – `GET /api/jobs/{id}/certificates`

Optional query params: `status=success|failed|pending`, `limit` (default 100, max 500), `offset`.
Every item has `position` (index in your original list), `status`, `error` (if failed) and, if successful, a `download_url`.

```bash
curl "http://127.0.0.1:5000/api/jobs/<id>/certificates?status=failed"
```

### 4. Retrieve certificates

```bash
# a single certificate (PDF)
curl -OJ http://127.0.0.1:5000/api/certificates/<certificate_id>/download

# every successful certificate of a job (ZIP)
curl -OJ http://127.0.0.1:5000/api/jobs/<job_id>/download
```

A certificate that failed or isn't ready yet returns `409` with the reason.

### Error responses

| Code | When |
|---|---|
| 400 | body is not valid JSON, or bad query parameter |
| 422 | request-level validation failed (details listed in `details`) |
| 404 | unknown job / certificate |
| 409 | certificate/ZIP not available (failed or still pending) |

## Design decisions

**Background processing (not synchronous).** A request may hold up to 1000 recipients, and rendering is CPU/disk
work. Doing it inside the HTTP request would tie up a web worker and risk client/proxy timeouts, so `POST /api/jobs`
persists the job, returns immediately with `202`, and a `ThreadPoolExecutor` generates certificates in the
background while the client polls `GET /api/jobs/{id}`. I chose an in-process thread pool over Celery/Redis to keep
the project runnable with zero infrastructure; the processing code (`service.process_job`) is independent of Flask, so
it can be moved to a real queue later by changing only `service.dispatch`.

**Two levels of validation.**
- *Request-level* problems (no event name, empty list, >1000 recipients, bad date) mean the request itself is
  unusable → `422`, nothing is stored.
- *Recipient-level* problems (blank name, bad email, wrong shape) must not block the others, so the job **is** created and
  that recipient is recorded as `failed` with a human-readable reason. The client gets one complete report instead of a
  rejected batch.

**Failure isolation.** Each certificate is rendered inside its own `try/except` and written in its own DB
transaction. An exception for one recipient marks only that row `failed`. PDFs are written to a temp file and moved with
`os.replace`, so a crash never leaves a half-written file that looks valid.

**Data model.** `jobs` (one per request) and `certificates` (one per recipient, with `position`, `status`, `error`,
`file_path`). Progress counts are computed from `certificates` with a `GROUP BY` rather than stored counters, so they can
never drift out of sync with the rows.

**Restart recovery.** On startup, jobs still `pending`/`processing` are re-dispatched; only certificates that
are still `pending` are processed, so completed work isn't redone.

**Template.** One fixed landscape A4 template drawn in code (`app/certificate.py`). Text is drawn directly on the canvas
rather than as markup, so names like `<b>` render literally, and long names shrink to fit.

**Files on disk, metadata in DB.** PDFs live under `data/certificates/<job_id>/<certificate_id>.pdf`; file names use IDs, never
user input, so there's no path-traversal risk. Download names are sanitised separately.

## Known limitations

- The thread pool is **in-process**: run a single server process (e.g. not several gunicorn workers), otherwise each
  would try to resume the same unfinished jobs. A real deployment would use a task queue.
- No authentication or per-client ownership of jobs; any caller who knows an ID can read it (IDs are random UUIDs).
- Built-in PDF fonts cover Western European text; CJK/Arabic names would need a TTF font registered in ReportLab.
- SQLite is fine for this scope; switching to PostgreSQL would mean replacing `app/db.py` (SQL used is standard).

## Project layout

```
app/
  __init__.py      Flask app factory + routes + error handlers
  service.py       job creation, background processing, queries
  validation.py    request- and recipient-level validation
  certificate.py   the PDF template
  db.py            SQLite schema + connection helper
tests/test_api.py  17 tests
run.py             dev server entrypoint
```
