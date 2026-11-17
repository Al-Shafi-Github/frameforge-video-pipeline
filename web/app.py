from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import studio_config
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from web.jobs import JobStore

STATIC = Path(__file__).parent / "static"
store = JobStore()


@asynccontextmanager
async def lifespan(app):
    store.start()
    yield
    store.close()


app = FastAPI(title="Frameforge Studio", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])


@app.middleware("http")
async def secure_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


def authorized(request: Request):
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin access is disabled")
    token = os.getenv("STUDIO_API_TOKEN", "")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token and not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "Enter the studio access token in Settings")


class VideoRequest(BaseModel):
    mode: Literal["story", "brand", "knowledge"] = "story"
    prompt: str = Field(min_length=8, max_length=5000)
    duration: int = Field(default=30, ge=1, le=300, strict=True)
    details: str = Field(default="", max_length=5000)
    label: str = Field(default="", max_length=55)
    voice: Literal["af_heart", "af_bella", "am_michael"] = "af_heart"

    @field_validator("prompt")
    @classmethod
    def clean_prompt(cls, value):
        if len(value.strip()) < 8:
            raise ValueError("Describe your idea in at least 8 characters")
        return value.strip()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config", dependencies=[Depends(authorized)])
def config():
    return {"max_duration": 300, "namespace": studio_config.NAMESPACE, "compute": "Modal only", "voices": ["af_heart", "af_bella", "am_michael"]}


@app.get("/api/jobs", dependencies=[Depends(authorized)])
def jobs():
    return store.list()


@app.post("/api/jobs", status_code=202, dependencies=[Depends(authorized)])
def create(payload: VideoRequest):
    try:
        return store.create(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from exc


def find(job_id):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/jobs/{job_id}", dependencies=[Depends(authorized)])
def job(job_id: str):
    return find(job_id)


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(authorized)])
def retry(job_id: str):
    find(job_id)
    try:
        return store.retry(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/jobs/{job_id}/assets/{name}", dependencies=[Depends(authorized)])
def asset(job_id: str, name: str):
    record = find(job_id)
    allowed = {"final.mp4", "preview_sheet.jpg", "report.json", "timeline.json", "facts.json"}
    if name not in allowed or record["status"] != "completed":
        raise HTTPException(404, "Asset unavailable")
    path = store.root / "outputs" / record["id"] / name
    if not path.is_file():
        raise HTTPException(404, "Asset not found")
    return FileResponse(path, filename=name if name != "final.mp4" else None)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
