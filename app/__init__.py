"""Application factory."""
import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, url_for

from . import db, service
from .validation import RequestValidationError, validate_request


def create_app(test_config=None):
    app = Flask(__name__)
    base = Path(__file__).resolve().parent.parent
    cfg = {"DB_PATH": str(base / "data" / "certgen.db"),
           "STORAGE_DIR": str(base / "data" / "certificates"),
           "RUN_ASYNC": True, "MAX_WORKERS": 2, "RECOVER_ON_START": True,
           "RENDERER": None}
    cfg.update(test_config or {})

    Path(cfg["DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg["STORAGE_DIR"]).mkdir(parents=True, exist_ok=True)
    db.init_db(cfg["DB_PATH"])

    settings = service.Settings(
        db_path=cfg["DB_PATH"], storage_dir=cfg["STORAGE_DIR"], run_async=cfg["RUN_ASYNC"],
        max_workers=cfg["MAX_WORKERS"], recover_on_start=cfg["RECOVER_ON_START"],
        executor=ThreadPoolExecutor(max_workers=cfg["MAX_WORKERS"], thread_name_prefix="certgen"))
    if cfg["RENDERER"]:
        settings.renderer = cfg["RENDERER"]
    app.extensions["certgen"] = settings

    # Jobs that were queued/running when the process died are resumed.
    if settings.run_async and settings.recover_on_start:
        for job_id in service.unfinished_job_ids(settings):
            service.dispatch(settings, job_id)

    register_routes(app, settings)
    register_errors(app)
    return app


def _cert_json(row):
    d = {"id": row["id"], "position": row["position"], "recipient_name": row["recipient_name"],
         "recipient_email": row["recipient_email"], "status": row["status"], "error": row["error"]}
    if row["status"] == service.C_SUCCESS:
        d["download_url"] = url_for("download_certificate", cert_id=row["id"])
    return d


def _safe_filename(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "certificate").strip("_")[:60] or "certificate"


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except ValueError:
        return default


def register_routes(app, settings):
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/jobs")
    def create_job():
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            return jsonify(error="request body must be valid JSON"), 400
        job_data, recipients = validate_request(payload)          # may raise -> 422
        job_id, has_work = service.create_job(settings, job_data, recipients)
        if has_work:
            service.dispatch(settings, job_id)
        body = service.job_summary(settings, job_id)
        body["links"] = {"self": url_for("get_job", job_id=job_id),
                         "certificates": url_for("list_job_certificates", job_id=job_id),
                         "download_all": url_for("download_job", job_id=job_id)}
        # 202 = accepted for background processing; 201 when already finished inline.
        done = body["status"] in (service.COMPLETED, service.COMPLETED_WITH_ERRORS, service.FAILED)
        return jsonify(body), (201 if done else 202)

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        summary = service.job_summary(settings, job_id)
        if summary is None:
            return jsonify(error="job not found"), 404
        return jsonify(summary)

    @app.get("/api/jobs/<job_id>/certificates")
    def list_job_certificates(job_id):
        if service.job_summary(settings, job_id) is None:
            return jsonify(error="job not found"), 404
        status = request.args.get("status")
        if status and status not in (service.C_PENDING, service.C_SUCCESS, service.C_FAILED):
            return jsonify(error="status must be one of: pending, success, failed"), 400
        limit, offset = _int_arg("limit", 100, 1, 500), _int_arg("offset", 0, 0, 10**9)
        rows = service.list_certificates(settings, job_id, status, limit, offset)
        return jsonify(certificates=[_cert_json(r) for r in rows], limit=limit, offset=offset)

    @app.get("/api/certificates/<cert_id>/download")
    def download_certificate(cert_id):
        row = service.get_certificate(settings, cert_id)
        if row is None:
            return jsonify(error="certificate not found"), 404
        if row["status"] != service.C_SUCCESS or not Path(row["file_path"] or "").is_file():
            return jsonify(error=f"certificate is not available (status: {row['status']})",
                           detail=row["error"]), 409
        return send_file(row["file_path"], mimetype="application/pdf", as_attachment=True,
                         download_name=f"{_safe_filename(row['recipient_name'])}.pdf")

    @app.get("/api/jobs/<job_id>/download")
    def download_job(job_id):
        if service.job_summary(settings, job_id) is None:
            return jsonify(error="job not found"), 404
        rows = service.list_certificates(settings, job_id, service.C_SUCCESS, limit=10**6)
        if not rows:
            return jsonify(error="no certificates have been generated for this job yet"), 409
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # PDFs are already compressed
            for r in rows:
                # position prefix keeps names unique even for duplicate recipient names
                zf.write(r["file_path"], f"{r['position'] + 1:04d}_{_safe_filename(r['recipient_name'])}.pdf")
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name=f"certificates_{job_id}.zip")


def register_errors(app):
    @app.errorhandler(RequestValidationError)
    def on_validation(e):
        return jsonify(error="validation failed", details=e.errors), 422

    @app.errorhandler(404)
    def on_404(_):
        return jsonify(error="not found"), 404

    @app.errorhandler(405)
    def on_405(_):
        return jsonify(error="method not allowed"), 405

    @app.errorhandler(Exception)
    def on_500(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify(error=e.description), e.code
        app.logger.exception("unhandled error")
        return jsonify(error="internal server error"), 500
