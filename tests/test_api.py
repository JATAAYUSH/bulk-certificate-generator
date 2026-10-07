import io
import shutil
import tempfile
import threading
import time
import unittest
import zipfile

from app import create_app, service
from app.certificate import render_certificate


def payload(recipients, **kw):
    base = {"event_name": "Python Bootcamp 2025", "issued_by": "Acme Academy",
            "issue_date": "2025-03-14", "recipients": recipients}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    run_async = False
    renderer = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, **extra):
        cfg = {"DB_PATH": f"{self.tmp}/t.db", "STORAGE_DIR": f"{self.tmp}/files",
               "RUN_ASYNC": self.run_async, "RENDERER": self.renderer}
        cfg.update(extra)
        return create_app(cfg)

    def tearDown(self):
        self.app.extensions["certgen"].executor.shutdown(wait=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_done(self, job_id, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            body = self.client.get(f"/api/jobs/{job_id}").get_json()
            if body["status"] in ("completed", "completed_with_errors", "failed"):
                return body
            time.sleep(0.05)
        self.fail("job did not finish in time")


class CreateJobTests(Base):
    def test_home_page_loads_browser_interface(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.content_type)
        self.assertIn(b"Bulk Certificate Generator", response.data)
        self.assertIn(b"Generate certificates", response.data)

    def test_create_job_sync_returns_finished_job(self):
        r = self.client.post("/api/jobs", json=payload([{"name": "Asha Patel", "email": "asha@x.com"},
                                                         {"name": "Ravi Shah"}]))
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["progress"]["total"], 2)
        self.assertEqual(body["progress"]["succeeded"], 2)
        self.assertIn("self", body["links"])

    def test_malformed_json_is_400(self):
        r = self.client.post("/api/jobs", data="{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_unknown_job_is_404(self):
        self.assertEqual(self.client.get("/api/jobs/nope").status_code, 404)


class AsyncCreateTests(Base):
    run_async = True

    def test_async_job_is_accepted_then_completes(self):
        r = self.client.post("/api/jobs", json=payload([{"name": f"User {i}"} for i in range(25)]))
        self.assertEqual(r.status_code, 202)
        done = self.wait_done(r.get_json()["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["progress"]["succeeded"], 25)
        self.assertEqual(done["progress"]["percent"], 100.0)


class ValidationTests(Base):
    def assert_422(self, body):
        r = self.client.post("/api/jobs", json=body)
        self.assertEqual(r.status_code, 422, r.get_json())
        self.assertTrue(r.get_json()["details"])

    def test_request_level_errors_reject_whole_request(self):
        self.assert_422([1, 2, 3])                                   # not an object
        self.assert_422(payload([{"name": "A"}], event_name=""))     # blank event
        self.assert_422(payload([{"name": "A"}], event_name=None))
        self.assert_422(payload([]))                                 # empty list
        self.assert_422(payload("A, B"))                             # wrong type
        self.assert_422(payload([{"name": "A"}], issue_date="14/03/2025"))
        self.assert_422(payload([{"name": "A"}] * 1001))             # too many

    def test_invalid_recipients_are_failed_not_fatal(self):
        r = self.client.post("/api/jobs", json=payload([
            {"name": "Good One", "email": "good@x.com"},
            {"name": "   "},                                  # blank name
            {"name": "Bad Email", "email": "not-an-email"},
            "just a string",                                  # wrong shape
            {"name": "x" * 101},                              # too long
            {"name": "Good Two"},
        ]))
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual(body["status"], "completed_with_errors")
        self.assertEqual(body["progress"]["succeeded"], 2)
        self.assertEqual(body["progress"]["failed"], 4)

        failed = self.client.get(f"/api/jobs/{body['id']}/certificates?status=failed").get_json()
        self.assertEqual([c["position"] for c in failed["certificates"]], [1, 2, 3, 4])
        self.assertIn("name is required", failed["certificates"][0]["error"])
        self.assertIn("email", failed["certificates"][1]["error"])

    def test_all_invalid_recipients_marks_job_failed(self):
        r = self.client.post("/api/jobs", json=payload([{"name": ""}, {}]))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["status"], "failed")

    def test_bad_status_filter_is_400(self):
        job = self.client.post("/api/jobs", json=payload([{"name": "A"}])).get_json()
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/certificates?status=bogus").status_code, 400)


class GenerationTests(Base):
    def test_generated_file_is_a_pdf_containing_recipient_details(self):
        job = self.client.post("/api/jobs", json=payload([{"name": "Zoë O'Brien <b>"}])).get_json()
        cert = self.client.get(job["links"]["certificates"]).get_json()["certificates"][0]
        r = self.client.get(cert["download_url"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        data = r.data
        r.close()
        self.assertTrue(data.startswith(b"%PDF"))
        try:
            from pypdf import PdfReader
        except ImportError:
            return  # content check is optional
        reader = PdfReader(io.BytesIO(data))
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        self.assertIn("Zoë O'Brien <b>", text)        # markup-like text rendered literally
        self.assertIn("Python Bootcamp 2025", text)
        self.assertIn("14 March 2025", text)
        self.assertIn("Acme Academy", text)

    def test_very_long_name_still_renders(self):
        r = self.client.post("/api/jobs", json=payload([{"name": "W" * 100}]))
        self.assertEqual(r.get_json()["progress"]["succeeded"], 1)


class FailingRendererTests(Base):
    @staticmethod
    def renderer(path, **kw):
        if kw["recipient_name"] == "Boom":
            raise RuntimeError("disk exploded")
        return render_certificate(path, **kw)

    def test_one_failure_does_not_stop_others(self):
        job = self.client.post("/api/jobs", json=payload(
            [{"name": "Before"}, {"name": "Boom"}, {"name": "After"}])).get_json()
        self.assertEqual(job["status"], "completed_with_errors")
        self.assertEqual((job["progress"]["succeeded"], job["progress"]["failed"]), (2, 1))

        certs = self.client.get(job["links"]["certificates"]).get_json()["certificates"]
        self.assertEqual([c["status"] for c in certs], ["success", "failed", "success"])
        self.assertIn("disk exploded", certs[1]["error"])
        self.assertNotIn("download_url", certs[1])

        # the failed certificate cannot be downloaded; the others can
        failed_id = certs[1]["id"]
        self.assertEqual(self.client.get(f"/api/certificates/{failed_id}/download").status_code, 409)
        ok = self.client.get(certs[2]["download_url"])
        self.assertEqual(ok.status_code, 200)
        ok.close()

    def test_no_partial_files_left_behind(self):
        import os
        job = self.client.post("/api/jobs", json=payload([{"name": "Boom"}, {"name": "Ok"}])).get_json()
        files = os.listdir(f"{self.tmp}/files/{job['id']}")
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(".pdf"))


class ProgressTests(Base):
    run_async = True
    gate = threading.Event()
    started = threading.Event()

    @staticmethod
    def renderer(path, **kw):
        ProgressTests.started.set()
        ProgressTests.gate.wait(5)
        return render_certificate(path, **kw)

    def test_progress_visible_while_running(self):
        ProgressTests.gate.clear()
        ProgressTests.started.clear()
        r = self.client.post("/api/jobs", json=payload([{"name": "A"}, {"name": "B"}, {"name": ""}]))
        self.assertEqual(r.status_code, 202)
        job_id = r.get_json()["id"]
        self.assertTrue(ProgressTests.started.wait(5))

        mid = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(mid["status"], "processing")
        self.assertEqual(mid["progress"]["total"], 3)
        self.assertEqual(mid["progress"]["failed"], 1)      # invalid row is already known
        self.assertEqual(mid["progress"]["pending"], 2)
        self.assertLess(mid["progress"]["percent"], 100)

        # downloading while nothing is ready yet is a 409, not a crash
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/download").status_code, 409)

        ProgressTests.gate.set()
        done = self.wait_done(job_id)
        self.assertEqual(done["status"], "completed_with_errors")
        self.assertEqual(done["progress"]["percent"], 100.0)


class RetrievalTests(Base):
    def setUp(self):
        super().setUp()
        self.job = self.client.post("/api/jobs", json=payload(
            [{"name": "Same Name"}, {"name": "Same Name"}, {"name": ""}, {"name": "Third"}])).get_json()

    def test_list_and_paginate(self):
        url = self.job["links"]["certificates"]
        self.assertEqual(len(self.client.get(url).get_json()["certificates"]), 4)
        page = self.client.get(url + "?limit=2&offset=1").get_json()["certificates"]
        self.assertEqual([c["position"] for c in page], [1, 2])
        ok = self.client.get(url + "?status=success").get_json()["certificates"]
        self.assertEqual(len(ok), 3)

    def test_zip_contains_only_successful_certificates(self):
        r = self.client.get(self.job["links"]["download_all"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = zf.namelist()
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)               # duplicate names don't collide
        self.assertTrue(all(zf.read(n).startswith(b"%PDF") for n in names))

    def test_unknown_ids(self):
        self.assertEqual(self.client.get("/api/certificates/nope/download").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/nope/download").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/nope/certificates").status_code, 404)


class RecoveryTests(Base):
    def test_unfinished_job_is_resumed_on_startup(self):
        # Simulate a crash: job is stored but never processed.
        settings = self.app.extensions["certgen"]
        job_id, _ = service.create_job(
            settings, {"event_name": "E", "issued_by": None, "issue_date": "2025-01-01"},
            [{"name": "A"}, {"name": "B"}])
        self.assertEqual(service.job_summary(settings, job_id)["status"], "pending")

        app2 = self.make_app(RUN_ASYNC=True)               # "restart" with same DB
        try:
            client2 = app2.test_client()
            end = time.time() + 10
            while time.time() < end:
                body = client2.get(f"/api/jobs/{job_id}").get_json()
                if body["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["progress"]["succeeded"], 2)
        finally:
            app2.extensions["certgen"].executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
