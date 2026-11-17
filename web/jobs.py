"""Single-host durable job queue. No inference or media processing happens here."""
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
STAGES = {"facts": (8, "Grounding your brief"), "outline": (15, "Planning the narrative"),
          "script": (22, "Writing the script"), "content_validation": (28, "Checking scene content"),
          "script_quality": (32, "Reviewing the script"), "narration": (42, "Creating narration"),
          "final_script_quality": (55, "Checking the final copy"), "images": (60, "Generating scene images"),
          "render": (82, "Animating the scenes"), "assembly": (92, "Assembling your video"),
          "validation": (96, "Validating playback")}
MODES = {
    "story": "Create a storytelling video with an evolving narrative, distinct scenes and a satisfying resolution.",
    "brand": "Create a brand promotion or sales campaign. Use only supplied product attributes, prices, currency and deadlines. Never invent these facts. Use varied product views and a clear invitation.",
    "knowledge": "Create an accessible educational explainer with a clear introduction, distinct learning points and a concise summary. Avoid unsupported claims. Do not invent sources."
}


class JobStore:
    def __init__(self, root=None):
        self.root = Path(root or ROOT / "data")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "jobs.sqlite3"
        self.pending = queue.Queue()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.process = None
        self.thread = None
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created REAL, updated REAL, status TEXT, payload TEXT, progress INTEGER, stage TEXT, error TEXT)")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, job_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        row["elapsed_seconds"] = int(time.time() - row["created"]) if row["status"] in ("queued", "running") else int(row["updated"] - row["created"])
        row["estimate"] = self.estimate(row["payload"]["duration"])
        return row

    def list(self):
        with self.connect() as db:
            ids = db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 30").fetchall()
        return [self.get(i[0]) for i in ids]

    def estimate(self, duration):
        # Initial range is deliberately broad; cold starts dominate small videos.
        return {"low_minutes": max(4, round(duration / 30 * 2)),
                "high_minutes": max(15, round(duration / 30 * 7)),
                "basis": "Planning estimate; GPU cold starts, retries and queueing can take longer."}

    def update(self, job_id, **fields):
        allowed = {"status", "progress", "stage", "error"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid update")
        with self.connect() as db:
            db.execute("UPDATE jobs SET updated=?, " + ", ".join(f"{key}=?" for key in fields) + " WHERE id=?", (time.time(), *fields.values(), job_id))

    def create(self, payload):
        with self.lock:
            with self.connect() as db:
                active = db.execute("SELECT id,payload FROM jobs WHERE status IN ('queued','running')").fetchall()
                for job_id, encoded in active:
                    if json.loads(encoded) == payload:
                        return self.get(job_id)
                if len(active) >= 5:
                    raise ValueError("Queue is full. Wait for a video to finish before adding another.")
                job_id, now = uuid.uuid4().hex, time.time()
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", (job_id, now, now, "queued", json.dumps(payload), 0, "Waiting for a worker", None))
            self.pending.put(job_id)
        return self.get(job_id)

    def retry(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if not job or job["status"] not in ("failed", "interrupted"):
                raise ValueError("Only failed or interrupted jobs can be resumed.")
            self.update(job_id, status="queued", error=None, stage="Queued to resume from saved checkpoints")
            self.pending.put(job_id)
        return self.get(job_id)

    def start(self):
        # A terminated local process cannot be assumed to have cancelled Modal.
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted', stage='Interrupted; inspect Modal before resuming', updated=? WHERE status='running'", (time.time(),))
            ids = db.execute("SELECT id FROM jobs WHERE status='queued'").fetchall()
        for (job_id,) in ids:
            self.pending.put(job_id)
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
        if self.thread:
            self.thread.join(timeout=5)

    def loop(self):
        while not self.stop.is_set():
            try:
                job_id = self.pending.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                self.run(job_id)
            except Exception:
                self.update(job_id, status="failed", stage="Generation stopped", error="The generation process could not finish. Check the private job log and Modal dashboard, then resume.")
            finally:
                self.pending.task_done()

    def run(self, job_id):
        job = self.get(job_id)
        if not job or job["status"] != "queued":
            return
        payload = job["payload"]
        folder = self.root / "outputs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "worker.log"
        self.update(job_id, status="running", progress=3, stage="Connecting to Modal · cold starts may take several minutes")
        prompt = MODES[payload["mode"]] + "\nUser brief: " + payload["prompt"]
        args = [sys.executable, "-u", "-X", "utf8", str(ROOT / "main.py"), prompt,
                "--duration", str(payload["duration"]), "--voice", payload["voice"],
                "--output-dir", str(folder)]
        if payload.get("details"):
            args += ["--details", payload["details"]]
        if payload.get("label"):
            args += ["--label", payload["label"]]
        # No shell interpolation. The browser never receives credentials.
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "TERM": "dumb"}
        self.process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)
        try:
            with log.open("a", encoding="utf-8") as handle:
                for line in self.process.stdout:
                    handle.write(line)
                    handle.flush()
                    # Only JSON events from the actual remote pipeline advance progress.
                    at = line.find('{"timestamp"')
                    if at >= 0:
                        try:
                            event = json.loads(line[at:])
                            if event.get("event") == "stage_started" and event.get("stage") in STAGES:
                                progress, title = STAGES[event["stage"]]
                                self.update(job_id, progress=progress, stage=title)
                        except (ValueError, KeyError):
                            pass
            code = self.process.wait()
            if self.stop.is_set():
                self.update(job_id, status="interrupted", stage="Interrupted; inspect Modal before resuming")
            elif code == 0 and (folder / "final.mp4").exists() and (folder / "report.json").exists():
                self.update(job_id, status="completed", progress=100, stage="Your video is ready")
            else:
                self.update(job_id, status="failed", stage="Generation stopped", error="Modal could not complete this attempt. Verify credentials, worker deployments and available credits. Saved checkpoints can be resumed.")
        finally:
            self.process.stdout.close()
            self.process = None
