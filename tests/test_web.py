import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
from web import app as webapp
from web.jobs import JobStore, MODES


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JobStore(self.temp.name)
        self.store_patch = patch.object(webapp, "store", self.store)
        self.store_patch.start()
        self.env = patch.dict(os.environ, {"STUDIO_API_TOKEN": ""})
        self.env.start()
        # Do not start lifespan: these tests never submit real Modal work.
        self.client = TestClient(webapp.app)
        self.payload = {"mode": "story", "prompt": "A robot explores a moon garden", "duration": 30, "voice": "af_heart", "label": "", "details": ""}

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    def test_static_interface_and_security_headers(self):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn("The screening room", result.text)
        self.assertIn("frame-ancestors 'none'", result.headers["content-security-policy"])
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_create_default_duration_and_persist(self):
        payload = {"prompt": "Tell a story about a kind robot"}
        response = self.client.post("/api/jobs", json=payload)
        self.assertEqual(response.status_code, 202)
        job = response.json()
        self.assertEqual(job["payload"]["duration"], 30)
        other = JobStore(self.temp.name)
        self.assertEqual(other.get(job["id"])["status"], "queued")

    def test_all_modes_and_limits(self):
        for mode in MODES:
            self.assertEqual(self.client.post("/api/jobs", json={**self.payload, "mode": mode}).status_code, 202)
        for value in (0, 301, 2.5):
            self.assertEqual(self.client.post("/api/jobs", json={**self.payload, "duration": value}).status_code, 422)
        self.assertEqual(self.client.post("/api/jobs", json={**self.payload, "mode": "unknown"}).status_code, 422)
        self.assertEqual(self.client.post("/api/jobs", json={**self.payload, "prompt": "        "}).status_code, 422)

    def test_duplicates_and_queue_bound(self):
        first = self.client.post("/api/jobs", json=self.payload).json()
        duplicate = self.client.post("/api/jobs", json=self.payload).json()
        self.assertEqual(first["id"], duplicate["id"])
        for n in range(4):
            self.client.post("/api/jobs", json={**self.payload, "prompt": f"A different story about planet {n}"})
        self.assertEqual(self.client.post("/api/jobs", json={**self.payload, "prompt": "Sixth new video about a robot"}).status_code, 429)

    def test_auth_and_cross_origin(self):
        with patch.dict(os.environ, {"STUDIO_API_TOKEN": "test-only-token"}):
            self.assertEqual(self.client.get("/api/jobs").status_code, 401)
            self.assertEqual(self.client.get("/api/jobs", headers={"Authorization": "Bearer test-only-token"}).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs", json=self.payload, headers={"Origin": "https://other.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/jobs", headers={"Host": "other.example"}).status_code, 400)

    def test_retry_and_missing_job(self):
        job = self.store.create(self.payload)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/retry").status_code, 409)
        self.store.update(job["id"], status="failed")
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/retry").status_code, 200)
        self.assertEqual(self.client.get("/api/jobs/missing").status_code, 404)

    def test_asset_allowlist(self):
        job = self.store.create(self.payload)
        self.store.update(job["id"], status="completed")
        folder = self.store.root / "outputs" / job["id"]
        folder.mkdir(parents=True)
        (folder / "report.json").write_text('{"status":"completed"}')
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/assets/report.json").status_code, 200)
        for name in ("worker.log", ".env", "missing.json"):
            self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/assets/{name}").status_code, 404)

    def test_mocked_runner_records_real_stage_events_and_completion(self):
        job = self.store.create(self.payload)
        folder = self.store.root / "outputs" / job["id"]
        folder.mkdir(parents=True)
        (folder / "final.mp4").write_bytes(b"test-only-not-real-media")
        (folder / "report.json").write_text('{"status":"completed"}')
        process = Mock(stdout=io.StringIO('{"timestamp":"now","event":"stage_started","stage":"images"}\n'))
        process.wait.return_value = 0
        with patch("web.jobs.subprocess.Popen", return_value=process) as spawn:
            self.store.run(job["id"])
            command = spawn.call_args.args[0]
            self.assertIn("--duration", command)
            self.assertIn("30", command)
            self.assertIn("User brief", command[5])
        self.assertEqual(self.store.get(job["id"])["status"], "completed")
        self.assertTrue((folder / "worker.log").exists())

    def test_restart_marks_running_jobs_interrupted(self):
        job = self.store.create(self.payload)
        self.store.update(job["id"], status="running")
        fresh = JobStore(self.temp.name)
        fresh.start()
        try:
            self.assertEqual(fresh.get(job["id"])["status"], "interrupted")
        finally:
            fresh.close()


if __name__ == "__main__":
    unittest.main()
