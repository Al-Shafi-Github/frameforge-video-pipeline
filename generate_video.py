"""Submit a title/prompt to Modal and download a complete narrated video."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile

from video_plan import make_brief, job_hash, content_is_valid, RENDER_REVISION
from video_quality import quality_cache_valid
from studio_config import JOBS_VOLUME


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="Any title, story, documentary, advertisement, or campaign brief")
    parser.add_argument("--duration", default=None, help="1–300 seconds: 45, '90s', '2 minutes'; default 30s")
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--details", default="", help="Optional factual details or approved campaign offer")
    parser.add_argument("--product", default=None, help="Optional subject/product name appended to the brief")
    parser.add_argument("--label", default=None, help="Optional top-left label; otherwise inferred by the model")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--provider", choices=["modal"], default="modal")
    parser.add_argument("--script-only", action="store_true", help="Validate script on Modal without spending on images, TTS or rendering")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    prompt = args.prompt + (f"\nSubject/product: {args.product}" if args.product else "")
    try:
        brief = make_brief(prompt, args.duration, args.voice, args.details, args.label)
        if args.label is not None and not 1 <= len(args.label.strip()) <= 55:
            raise ValueError("--label must contain 1–55 characters.")
    except ValueError as exc:
        parser.error(str(exc))
    return run_video(brief, args.output_dir, script_only=args.script_only)


def run_video(brief, output_dir=None, script_only=False):
    """Shared production API used by the CLI and Python/application callers."""
    brief = make_brief(brief["prompt"], brief.get("duration"), brief.get("voice", "af_heart"),
                       brief.get("details", ""), brief.get("label"))
    fingerprint = job_hash(brief)
    slug = re.sub(r"[^a-z0-9]+", "-", brief["prompt"].lower()).strip("-")[:55] or "video"
    output = Path(output_dir or f"workspace/videos/{slug}-{brief['duration']}s-{fingerprint[:8]}").resolve()
    video_path, report_path = output / "final.mp4", output / "report.json"
    if report_path.exists():
        saved = json.loads(report_path.read_text())
        if saved.get("job_id") != fingerprint:
            raise ValueError("Output folder belongs to another brief. Choose a different --output-dir.")
        # The timeline is the exact copy/visual plan used for the completed render.
        script_path = output / "timeline.json"
        if not script_path.exists():
            script_path = output / "script.json"
        content_valid = script_path.exists() and content_is_valid(json.loads(script_path.read_text(encoding="utf-8")))
        if not script_only and content_valid and quality_cache_valid(saved, json.loads(script_path.read_text(encoding="utf-8"))) and video_path.exists() and saved.get("render_revision") == RENDER_REVISION and saved.get("status") == "completed" and hashlib.sha256(video_path.read_bytes()).hexdigest() == saved.get("video_sha256"):
            print(f"Reusing completed video: {video_path}")
            return str(video_path)
    import modal
    from modal_workers.video import app, generate
    mode = "script-only review" if script_only else "prompt-driven video"
    print(f"Submitting {brief['duration']}s {mode} to Modal. Job: {fingerprint[:12]}")
    try:
        with modal.enable_output(), app.run():
            result = generate.remote(brief, script_only=script_only)
    except Exception:
        print(f"\nJob failed: {fingerprint}\nModal Volume: {JOBS_VOLUME}/{fingerprint}/failure.json\n"
              "Repeat the identical command to resume; completed assets remain checkpointed.", file=sys.stderr)
        raise
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(result["archive"])) as archive:
        for item in archive.infolist():
            target = (output / item.filename).resolve()
            if os.path.commonpath([str(output), str(target)]) != str(output):
                raise ValueError("Unsafe archive path.")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
    if script_only:
        print(f"\nValidated script: {output / 'script.json'}\nFacts: {output / 'facts.json'}\nQuality: {output / 'script_quality.json'}\nNo new video generated.")
    else:
        print(f"\nVideo: {video_path}\nScript: {output / 'script.json'}\nReport: {report_path}")
    print(json.dumps(result["report"], indent=2))
    return str(output / "script.json" if script_only else video_path)


if __name__ == "__main__":
    main()
