"""Input validation.

Two levels, deliberately different in how they fail:

* Request level  (bad JSON shape, missing event_name, empty/oversized list):
  the whole request is rejected with 422 and nothing is stored.
* Recipient level (blank name, malformed email, ...): the job IS created and the
  bad recipient is recorded as a failed certificate with a reason, so one bad
  row never blocks the other valid ones.
"""
import re
from datetime import date

MAX_RECIPIENTS = 1000
MAX_NAME_LEN = 100
MAX_EVENT_LEN = 150
# Deliberately simple: one @, no spaces, a dot in the domain.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RequestValidationError(Exception):
    def __init__(self, errors):
        super().__init__("invalid request")
        self.errors = errors


def _clean_str(value):
    return value.strip() if isinstance(value, str) else None


def validate_request(payload):
    """Validate the top-level request. Returns (job_data, recipients_raw)."""
    errors = []
    if not isinstance(payload, dict):
        raise RequestValidationError(["request body must be a JSON object"])

    event_name = _clean_str(payload.get("event_name"))
    if not event_name:
        errors.append("event_name is required and must be a non-empty string")
    elif len(event_name) > MAX_EVENT_LEN:
        errors.append(f"event_name must be at most {MAX_EVENT_LEN} characters")

    issued_by = payload.get("issued_by")
    if issued_by is not None:
        issued_by = _clean_str(issued_by)
        if issued_by is None:
            errors.append("issued_by must be a string")
        elif len(issued_by) > MAX_EVENT_LEN:
            errors.append(f"issued_by must be at most {MAX_EVENT_LEN} characters")

    issue_date = payload.get("issue_date")
    if issue_date is None:
        issue_date = date.today().isoformat()
    else:
        try:
            issue_date = date.fromisoformat(issue_date).isoformat()
        except (TypeError, ValueError):
            errors.append("issue_date must be an ISO date, e.g. 2025-01-31")

    recipients = payload.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        errors.append("recipients is required and must be a non-empty list")
    elif len(recipients) > MAX_RECIPIENTS:
        errors.append(f"at most {MAX_RECIPIENTS} recipients are allowed per request")

    if errors:
        raise RequestValidationError(errors)

    return {"event_name": event_name, "issued_by": issued_by or None,
            "issue_date": issue_date}, recipients


def validate_recipient(raw):
    """Validate one recipient. Returns (clean_dict, error_message_or_None).

    The cleaned dict is returned even on failure so the failed row can still
    show whatever name/email was supplied.
    """
    if not isinstance(raw, dict):
        return {"name": None, "email": None}, "recipient must be an object with a 'name'"

    name = _clean_str(raw.get("name"))
    email = raw.get("email")
    clean = {"name": name, "email": email.strip() if isinstance(email, str) else None}

    problems = []
    if not name:
        problems.append("name is required and must be a non-empty string")
    elif len(name) > MAX_NAME_LEN:
        problems.append(f"name must be at most {MAX_NAME_LEN} characters")
    elif any(ord(c) < 32 for c in name):
        problems.append("name contains control characters")

    if email is not None:
        if not isinstance(email, str) or not EMAIL_RE.match(email.strip()):
            problems.append("email is not a valid email address")

    return clean, "; ".join(problems) or None
