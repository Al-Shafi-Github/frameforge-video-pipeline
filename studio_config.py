"""Shared naming; all portfolio resources are isolated from office resources."""
import os
import re
from pathlib import Path
try:
    from dotenv import load_dotenv
except ImportError:
    # Model containers use injected environment variables, not local .env files.
    pass
else:
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
NAMESPACE = os.getenv("FRAMEFORGE_NAMESPACE", "frameforge")
if not re.fullmatch(r"[a-z][a-z0-9-]{2,35}", NAMESPACE):
    raise ValueError("FRAMEFORGE_NAMESPACE must be 3–36 lowercase letters/digits/hyphens, starting with a letter")
LLM_APP = f"{NAMESPACE}-llm"
IMAGE_APP = f"{NAMESPACE}-image"
TTS_APP = f"{NAMESPACE}-tts"
VIDEO_APP = f"{NAMESPACE}-video"
JOBS_VOLUME = f"{NAMESPACE}-jobs"
HF_SECRET = f"{NAMESPACE}-huggingface"
