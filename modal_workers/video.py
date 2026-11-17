"""Resumable prompt-driven video production; ALL inference/media work is remote."""
import json
from pathlib import Path
import modal
from studio_config import VIDEO_APP, JOBS_VOLUME, LLM_APP, IMAGE_APP, TTS_APP, NAMESPACE

app = modal.App(VIDEO_APP)
runtime = (modal.Image.debian_slim(python_version="3.11")
           .apt_install("ffmpeg", "fonts-dejavu-core")
           .pip_install("Pillow==11.1.0")
           .pip_install("python-dotenv")
           .env({"FRAMEFORGE_NAMESPACE": NAMESPACE})
           .add_local_python_source("studio_config", "video_plan", "video_quality", "render", "modal_workers"))
volume = modal.Volume.from_name(JOBS_VOLUME, create_if_missing=True)


@app.function(image=runtime, cpu=4, memory=8192, timeout=7200, volumes={"/jobs": volume})
def generate(brief: dict, script_only: bool = False) -> dict:
    import hashlib
    import io
    import math
    import re
    import subprocess
    import time
    import traceback
    import wave
    import zipfile
    from datetime import datetime, timezone
    from PIL import Image, ImageDraw, ImageFont
    from video_plan import (make_brief, job_hash, parse_json, scene_slots, target_words,
                            validate_outline, validate_batch, narration_fits,
                            retiming_word_target, allocate_timeline, validate_image_prompt,
                            validate_spoken_narration, physical_visual_seed, visual_topic,
                            extend_narration, content_is_valid, needs_visual_grounding, measured_scene_slots, FPS, RENDER_REVISION, MOTIONS)
    from render.ad_motion import motion_filter
    from video_quality import (QUALITY_REVISION, PLAN_REVISION, FACT_INSTRUCTION, COPY_INSTRUCTION, REVIEW_INSTRUCTION,
        source_text, validate_facts, deterministic_issues, validate_review,
        script_fingerprint, quality_cache_valid, failure_record, numbers,
        verified_offer, offer_copy, bind_commercial_copy)

    brief = make_brief(brief["prompt"], brief.get("duration"), brief.get("voice", "af_heart"),
                       brief.get("details", ""), brief.get("label"))
    job_id = job_hash(brief)
    root = Path("/jobs") / job_id
    root.mkdir(parents=True, exist_ok=True)
    for name in ("batches", "images", "audio", "clips"):
        (root / name).mkdir(exist_ok=True)
    started, stage = time.monotonic(), "starting"
    slots = scene_slots(brief["duration"])
    final = root / "final.mp4"
    active = {"scene_id": None, "request": None, "attempt": None}

    def save(path, data):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        volume.commit()

    def event(action, **fields):
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "job_id": job_id,
                  "stage": stage, "event": action, **active, **fields}
        line = json.dumps(record, ensure_ascii=False)
        print(line, flush=True)
        with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        # Persist with the next checkpoint rather than committing on every cache hit.

    def stage_to(value):
        nonlocal stage
        stage = value
        active.update(scene_id=None, request=None, attempt=None)
        event("stage_started")
        save(root / "manifest.json", {"job_id": job_id, "stage": stage, "status": "running"})

    def scene_to(scene_id):
        active.update(scene_id=scene_id, request=None, attempt=None)
        save(root / "manifest.json", {"job_id": job_id, "stage": stage,
                                     "status": "running", **active})

    def run(args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise RuntimeError(result.stderr[-6000:])
        return result.stdout

    def ask(context, instruction, validator, name):
        active["request"] = name
        if name.startswith("chapter_"):
            for previous in sorted((root / "batches").glob(f"{name}_attempt_*.json"), reverse=True):
                try:
                    recovered = validator(json.loads(previous.read_text()))
                    event("draft_recovered", request=name, candidate=previous.name)
                    return recovered
                except (ValueError, TypeError, KeyError):
                    continue
        feedback = ""
        for attempt in range(1, 4):
            active["attempt"] = attempt
            save(root / "manifest.json", {"job_id": job_id, "stage": stage,
                                         "status": "running", **active})
            try:
                raw = llm.generate.remote(json.dumps(context, ensure_ascii=False), instruction + feedback)
                (root / "batches" / f"{name}_attempt_{attempt}.txt").write_text(raw, encoding="utf-8")
                answer = parse_json(raw)
                save(root / "batches" / f"{name}_attempt_{attempt}.json", answer)
                validated = validator(answer)
                active.update(request=None, attempt=None)
                return validated
            except Exception as exc:
                event("json_attempt_failed", request=name, attempt=attempt, error=str(exc))
                feedback = f" Previous result failed validation: {exc}. Correct the entire JSON."
                if attempt == 3:
                    raise

    def review_script(phase, repair=True):
        """Review the entire copy, repair only flagged IDs, checkpoint each repair."""
        stage_to(phase)
        if repair:
            bound = bind_commercial_copy(script, facts, brief)
            if facts["video_type"] in ("advertisement", "campaign"):
                angles = ("Three-quarter hero perspective", "Detailed macro perspective",
                          "Low side-profile perspective", "Wide closing composition")
                for index, scene in enumerate(script["scenes"]):
                    prefix = f"{angles[index % len(angles)]} of {facts['subject']}. "
                    if not scene["image_prompt"].startswith(prefix):
                        scene["image_prompt"] = prefix + scene["image_prompt"]
                        bound.append(scene["id"])
            if bound:
                save(root / "script.json", script)
                event("source_facts_bound", scene_ids=bound)
        review_path = root / f"{phase}.json"
        if review_path.exists():
            cached = json.loads(review_path.read_text())
            if quality_cache_valid(cached, script) and not cached.get("issues"):
                event("quality_review_reused")
                return cached
        warnings = []
        for cycle in range(2 if repair else 1):
            issues = deterministic_issues(script, facts, brief)
            try:
                semantic = ask({"source": source_text(brief), "facts": facts, "verified_calculations": verified_offer(facts),
                                "scenes": [{"id": s["id"], "narration": s["narration"]} for s in script["scenes"]]}, REVIEW_INSTRUCTION,
                               lambda data: validate_review(data, script), f"{phase}_{cycle+1}")
            except Exception as exc:
                semantic = []
                warnings.append({"code": "review_unavailable", "message": str(exc)})
                event("advisory_review_unavailable", error=str(exc))
            warnings.extend(semantic)
            review = {"quality_revision": QUALITY_REVISION,
                      "reviewed_script_sha256": script_fingerprint(script), "issues": issues, "warnings": warnings}
            save(review_path, review)
            if not issues and (not semantic or not repair or cycle == 1):
                event("quality_passed", advisory_warning_count=len(warnings))
                return review
            event("quality_rejected" if issues else "advisory_repair_requested", issues=issues)
            if issues and (not repair or cycle == 1):
                active.update(scene_id=issues[0]["scene_ids"][0], request=phase, attempt=cycle+1)
                raise RuntimeError(f"Whole-script quality rejected: {issues}. Saved assets remain resumable.")
            suggestions = issues + semantic
            ids = {sid for issue in suggestions for sid in issue["scene_ids"]}
            # Only flagged scenes are rewritten; successful assets use content keys.
            for index, scene in enumerate(script["scenes"]):
                if scene["id"] not in ids:
                    continue
                if facts["video_type"] in ("advertisement", "campaign") and index == max(0, len(slots)-2) and offer_copy(facts):
                    # Offer values are bound from source, not rewritten by an
                    # unreliable critic. Cosmetic objections remain warnings.
                    event("locked_offer_preserved", scene_id=scene["id"])
                    continue
                scene_to(scene["id"])
                context = {"source": source_text(brief), "facts": facts,
                    "whole_spoken_copy": [{"id": s["id"], "narration": s["narration"]} for s in script["scenes"]],
                    "scene_id": scene["id"], "current_narration": scene["narration"],
                    "scene_role": ("opening hook" if index == 0 else
                        "closing invitation without repeating the offer" if index == len(slots)-1 and facts["video_type"] in ("advertisement", "campaign") else
                        "supplied offer" if index == len(slots)-2 and facts["video_type"] in ("advertisement", "campaign") else
                        "distinct product or story development"),
                    "target_words": target_words(slots[index]),
                    "issues": [issue for issue in suggestions if scene["id"] in issue["scene_ids"]]}
                def check_scene(data):
                    fixed = validate_batch({"scenes": [{**scene, "narration": data.get("narration")}]}, [slots[index]], index, draft=True)[0]
                    validate_spoken_narration(fixed["narration"])
                    candidate = {**script, "scenes": [fixed if s["id"] == fixed["id"] else s for s in script["scenes"]]}
                    local = [issue for issue in deterministic_issues(candidate, facts, brief)
                             if issue["code"] in ("currency", "numbers", "changed_price", "duplicate_copy") and fixed["id"] in issue["scene_ids"]]
                    if local:
                        raise ValueError(f"Repair still violates constraints: {local}")
                    if context["scene_role"] == "supplied offer":
                        for fact in facts["facts"]:
                            if fact["field"] in ("current_price", "previous_price", "offer_deadline", "discount", "offer_condition"):
                                if numbers(fact["value"]) - numbers(fixed["narration"]):
                                    raise ValueError(f"Offer scene must preserve {fact['field']}: {fact['value']}; closing invitation must not carry the missing offer.")
                    return fixed
                try:
                    fixed = ask(context,
                    'Return ONLY JSON {"narration":"..."}. You are writing words spoken to the audience, NOT scene descriptions. '
                    "Repair only this scene in the whole-script context. Preserve its distinct purpose and "
                    "Follow scene_role strictly. A closing invitation should not repeat prices/deadlines "
                    "already present in the offer scene. "
                    "all supplied facts. Do not restate the offer in every scene. Unknown currency stays "
                    "unspecified. Do not add prices, deadlines or savings to this non-offer scene. "
                    "Write engaging spoken copy, not shot descriptions. Do not copy visual "
                    "instructions from scene_beat or image_prompt into narration. "
                    f"Use between {max(1, math.floor(target_words(slots[index])*.6))} and {math.ceil(target_words(slots[index])*1.5)} words, aiming at target_words.",
                    check_scene, f"quality_repair_{phase}_{scene['id']}_{cycle+1}")
                except Exception as exc:
                    warnings.append({"scene_ids": [scene["id"]], "code": "kept_best_draft", "message": str(exc)})
                    event("repair_failed_kept_best_draft", error=str(exc))
                    continue
                if any(issue["code"].startswith(("visual", "image", "subject")) for issue in context["issues"]):
                    try:
                        fixed["image_prompt"] = ask({"subject": facts["subject"], "facts": facts,
                            "physical_visual_seed": physical_visual_seed(scene["image_prompt"]),
                            "style": outline["visual_style"]},
                            'Return ONLY JSON {"image_prompt":"..."}. Describe the physical subject in a '
                            "distinct still photograph, naming the subject and its appearance, setting and light. "
                            "Do not describe written words, fonts, prices, signs or countdown graphics.",
                            lambda data: validate_image_prompt(data.get("image_prompt")),
                            f"quality_visual_{scene['id']}_{cycle+1}")
                    except Exception as exc:
                        warnings.append({"scene_ids": [scene["id"]], "code": "kept_best_visual_prompt", "message": str(exc)})
                        event("visual_repair_failed_kept_best_prompt", error=str(exc))
                script["scenes"][index] = fixed
                bind_commercial_copy(script, facts, brief)
                save(root / "script.json", script)
                event("quality_scene_repaired")

    def script_bundle(review):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in [root / "brief.json", root / "facts.json", root / "outline.json",
                         root / "script.json", root / "script_quality.json", root / "events.jsonl"]:
                if path.exists():
                    archive.write(path, path.name)
        return {"archive": output.getvalue(), "report": {"status": "script_ready",
            "job_id": job_id, "quality_revision": QUALITY_REVISION,
            "reviewed_script_sha256": review["reviewed_script_sha256"],
            "scene_count": len(slots), "execution_location": "Modal", "video_created": False}}

    def wav_seconds(path):
        with wave.open(str(path), "rb") as wav:
            if wav.getframerate() != 24000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2 or not wav.getnframes():
                raise ValueError("Expected nonempty 24 kHz mono PCM narration.")
            return wav.getnframes() / 24000

    def synthesize(text, scene_id, speed=1.0):
        speed = round(speed, 4)
        key = hashlib.sha256(json.dumps([text, brief["voice"], speed, "kokoro-trim-v1"]).encode()).hexdigest()[:24]
        path = root / "audio" / f"{key}.wav"
        meta = path.with_suffix(".json")
        if path.exists() and meta.exists():
            try:
                seconds = wav_seconds(path)
                event("audio_reused", scene_id=scene_id, speed=speed)
                return path, seconds
            except (ValueError, wave.Error, EOFError):
                event("invalid_cached_audio", scene_id=scene_id)
        for attempt in range(1, 4):
            active["attempt"] = attempt
            try:
                begin = time.monotonic()
                raw_path = path.with_suffix(".raw.wav")
                raw_path.write_bytes(tts.generate.remote(text, brief["voice"], speed))
                duration = wav_seconds(raw_path)
                detected = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(raw_path),
                    "-af", "silencedetect=noise=-40dB:d=0.15", "-f", "null", "-"],
                    capture_output=True, text=True, check=True, timeout=120)
                intervals, beginning = [], None
                for line in detected.stderr.splitlines():
                    match = re.search(r"silence_start: ([\d.]+)", line)
                    if match:
                        beginning = float(match.group(1))
                    match = re.search(r"silence_end: ([\d.]+)", line)
                    if match and beginning is not None:
                        intervals.append((beginning, min(duration, float(match.group(1)))))
                        beginning = None
                if beginning is not None:
                    intervals.append((beginning, duration))
                leading = intervals[0][1] if intervals and intervals[0][0] < .02 else 0
                trailing = duration - intervals[-1][0] if intervals and abs(intervals[-1][1]-duration) < .03 else 0
                start, end = max(0, leading-.08), min(duration, duration-trailing+.12)
                if end <= start:
                    raise ValueError("TTS produced no usable speech.")
                run(["ffmpeg", "-v", "error", "-y", "-i", str(raw_path), "-af",
                     f"atrim=start={start}:end={end},asetpts=PTS-STARTPTS", "-c:a", "pcm_s16le", str(path)])
                seconds = wav_seconds(path)
                save(meta, {"text": text, "voice": brief["voice"], "speed": speed,
                            "audio_seconds": seconds, "generation_seconds": round(time.monotonic()-begin, 3)})
                event("audio_completed", scene_id=scene_id, seconds=round(seconds, 3), speed=speed)
                return path, seconds
            except Exception as exc:
                event("audio_attempt_failed", scene_id=scene_id, attempt=attempt, error=str(exc))
                if attempt == 3:
                    raise
                time.sleep(attempt)

    def bundle(report):
        sheet_path, sheet_meta = root / "preview_sheet.jpg", root / "preview_sheet.json"
        matches = sheet_meta.exists() and json.loads(sheet_meta.read_text()).get("video_sha256") == report["video_sha256"]
        if not sheet_path.exists() or not matches:
            script = json.loads((root / "timeline.json").read_text())
            scenes = script["scenes"]
            selected = sorted({round(i * (len(scenes)-1) / max(1, min(6, len(scenes))-1)) for i in range(min(6, len(scenes)))})
            sheet = Image.new("RGB", (1280, math.ceil(len(selected)/2)*384), (235, 238, 245))
            drawing = ImageDraw.Draw(sheet)
            for tile, index in enumerate(selected):
                seconds = sum(s["duration"] for s in scenes[:index]) + scenes[index]["duration"]/2
                frame = root / "clips" / f"preview_{index}.png"
                run(["ffmpeg", "-v", "error", "-y", "-ss", str(seconds), "-i", str(final),
                     "-frames:v", "1", "-vf", "scale=640:360", str(frame)])
                x, y = (tile % 2)*640, (tile // 2)*384
                with Image.open(frame) as image:
                    sheet.paste(image.convert("RGB"), (x, y))
                drawing.text((x+8, y+362), f"Scene {index+1}: {scenes[index]['motion']}", fill=(8, 32, 77))
            sheet.save(sheet_path, quality=90)
            save(sheet_meta, {"video_sha256": report["video_sha256"]})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file() and "clips" not in path.relative_to(root).parts and path.suffix != ".tmp":
                    archive.write(path, str(path.relative_to(root)))
        return {"archive": output.getvalue(), "report": report}

    try:
        if final.exists() and (root / "report.json").exists():
            report = json.loads((root / "report.json").read_text())
            existing_script = root / "timeline.json"
            if not existing_script.exists():
                existing_script = root / "script.json"
            content_valid = existing_script.exists() and content_is_valid(json.loads(existing_script.read_text()))
            if not script_only and content_valid and quality_cache_valid(report, json.loads(existing_script.read_text())) and report.get("render_revision") == RENDER_REVISION and report.get("status") == "completed" and hashlib.sha256(final.read_bytes()).hexdigest() == report.get("video_sha256"):
                event("completed_job_reused")
                return bundle(report)
        save(root / "brief.json", brief)
        llm = modal.Cls.from_name(LLM_APP, "LLMWorker")()
        tts = modal.Cls.from_name(TTS_APP, "KokoroTTSWorker")()
        image_worker = modal.Cls.from_name(IMAGE_APP, "ImageWorker")()
        stage_to("facts")
        facts_path = root / "facts.json"
        if facts_path.exists():
            facts = validate_facts(json.loads(facts_path.read_text()), brief)
            event("facts_reused")
        else:
            facts = ask({"source": source_text(brief)}, FACT_INSTRUCTION,
                        lambda data: validate_facts(data, brief), "facts")
            save(facts_path, facts)
        stage_to("planning")
        outline_path = root / "outline.json"
        plan_meta = root / "plan_quality.json"
        current_plan = plan_meta.exists() and json.loads(plan_meta.read_text()).get("quality_revision") in (PLAN_REVISION, QUALITY_REVISION)
        if outline_path.exists() and current_plan:
            outline = validate_outline(json.loads(outline_path.read_text()), len(slots))
            event("outline_reused")
        else:
            outline = ask({"brief": brief, "structured_facts": facts}, (
                'Return ONLY a JSON object with keys title, label, visual_style and beats. '
                'title and label are short strings. visual_style is a concrete description of colours, '
                'lighting, medium and the specific appearance of recurring subjects. beats is a list of strings. '
                f"Create exactly {len(slots)} sequential beats for a {brief['duration']}-second video. "
                "Infer whether this is a story, explanation, documentary, product advertisement or campaign from the user's prompt. "
                "Follow that topic and requested tone; do not force a skincare/product format onto other topics. "
                "For advertisements/campaigns, use only benefits, prices, discounts and dates explicitly supplied; "
                "do not invent offers or factual efficacy claims. Fictional stories may invent fictional events. "
                "Plan visually distinct shots; establish the main subject immediately. Avoid unrelated filler. "
                "For commercial videos plan an engaging hook, product/context development, supplied offer "
                "and call to action with different scene purposes. Mention price/deadline in no more than "
                "two scenes per short video. Do not invent currency. For fiction use a narrative arc instead. "
                "Scene beats describe physical events/subjects, not captions, text overlays, discount signs or camera directions. "
                "The output is animated still images, not neural moving footage."
            ), lambda data: validate_outline(data, len(slots)), "outline")
            save(outline_path, outline)
            save(plan_meta, {"quality_revision": QUALITY_REVISION})
        script_path = root / "script.json"
        if script_path.exists() and current_plan and json.loads(script_path.read_text()).get("quality_revision") in (PLAN_REVISION, QUALITY_REVISION):
            saved = json.loads(script_path.read_text())
            scenes = validate_batch(saved, slots, draft=True)
            for new, old in zip(scenes, saved["scenes"]):
                new["tts_speed"] = old.get("tts_speed", 1.0)
            script = {**outline, "quality_revision": QUALITY_REVISION, "duration_seconds": brief["duration"], "scenes": scenes}
            event("script_reused")
        else:
            scenes = []
            for start in range(0, len(slots), 6):
                batch_slots = slots[start:start+6]
                batch_path = root / "batches" / f"chapter_q{QUALITY_REVISION}_{start//6+1:03d}.json"
                if batch_path.exists():
                    batch = validate_batch(json.loads(batch_path.read_text()), batch_slots, start, draft=True)
                    event("chapter_reused", chapter=start//6+1)
                else:
                    copy_path = root / "batches" / f"copy_q{QUALITY_REVISION}_{start//6+1:03d}.json"
                    def validate_copy(data):
                        lines = data.get("narrations") if isinstance(data, dict) else None
                        if not isinstance(lines, list) or len(lines) != len(batch_slots):
                            raise ValueError(f"Return exactly {len(batch_slots)} spoken narrations.")
                        for line, slot in zip(lines, batch_slots):
                            validate_spoken_narration(line)
                            if not 1 <= len(line.split()) <= max(60, math.ceil(target_words(slot)*1.5)):
                                raise ValueError(f"Return concise spoken copy aiming at {target_words(slot)} words, not a long paragraph; got {len(line.split())}.")
                        return lines
                    if copy_path.exists():
                        copy_lines = validate_copy(json.loads(copy_path.read_text()))
                        event("copy_reused", chapter=start//6+1)
                    else:
                        roles = []
                        commercial = facts["video_type"] in ("advertisement", "campaign")
                        for offset, slot in enumerate(batch_slots):
                            number = start + offset
                            role = ("opening" if number == 0 else
                                    "closing invitation" if commercial and number == len(slots)-1 else
                                    "supplied offer" if commercial and number == len(slots)-2 else
                                    "resolution" if number == len(slots)-1 else "development with a new idea")
                            roles.append({"role": role, "target_words": target_words(slot),
                                "minimum_words": max(1, math.floor(target_words(slot)*.6)),
                                "maximum_words": math.ceil(target_words(slot)*1.5)})
                        copy_lines = ask({"source": source_text(brief), "facts": facts,
                            "roles": roles, "previous_spoken_copy": [s["narration"] for s in scenes]},
                            COPY_INSTRUCTION, validate_copy, f"copy_q{QUALITY_REVISION}_{start//6+1:03d}")
                        save(copy_path, {"narrations": copy_lines})
                    context = {"brief": brief, "structured_facts": facts, "outline": outline, "start_scene": start+1,
                               "beats": outline["beats"][start:start+len(batch_slots)],
                               "approved_spoken_narrations": copy_lines,
                               "target_words_per_scene": [target_words(s) for s in batch_slots],
                               "previous_narration": [s["narration"] for s in scenes[-6:]]}
                    batch = ask(context, (
                        'Return ONLY JSON {"scenes":[{"narration":"complete spoken text",'
                        '"image_prompt":"detailed still-image description","motion":"supported motion"}]}. '
                        f"Return exactly {len(batch_slots)} scenes in the supplied beat order. "
                        "Copy approved_spoken_narrations into narration verbatim, in order. Do not write new narration. "
                        "Narration must be spoken English, without timestamps, stage directions, word counts or camera directions. "
                        "No invented factual product claims/offers beyond the brief. "
                        "Unknown currency stays unspecified. Every scene advances the video instead of "
                        "repeating prices/deadlines. Use the outline's distinct purpose and whole chapter context. "
                        "Give each scene a different relevant composition. Repeat shared visual descriptors for recurring subjects. "
                        "Describe only objects actually wanted in the image: avoid numeric layout instructions, "
                        "negative lists of unwanted branded objects, and writing that the model could print on an object. "
                        "Do not instruct images to draw narration or promotional text. "
                        f"Choose varied, scene-appropriate motion from {MOTIONS}."
                    ), lambda data: validate_batch({"scenes": [{**s, "narration": line}
                        for s, line in zip(data["scenes"], copy_lines)]} if len(data.get("scenes", [])) == len(copy_lines) else data,
                        batch_slots, start, draft=True), f"chapter_q{QUALITY_REVISION}_{start//6+1:03d}")
                    save(batch_path, {"scenes": batch})
                    event("chapter_completed", chapter=start//6+1)
                scenes.extend(batch)
            script = {**outline, "quality_revision": QUALITY_REVISION, "duration_seconds": brief["duration"], "scenes": scenes}
            save(script_path, script)
        save(plan_meta, {"quality_revision": QUALITY_REVISION})

        stage_to("content_validation")
        for index, scene in enumerate(script["scenes"]):
            scene_to(scene["id"])
            context = {"brief": brief, "structured_facts": facts, "whole_script": script["scenes"], "scene": scene, "visual_style": outline["visual_style"]}
            if needs_visual_grounding(scene["image_prompt"]):
                def check_grounded(data):
                    prompt = validate_image_prompt(data.get("image_prompt") if isinstance(data, dict) else None)
                    if needs_visual_grounding(prompt):
                        raise ValueError("Name each physical subject explicitly; image generation has no earlier-scene context.")
                    return prompt
                scene["image_prompt"] = ask({"visual_style": outline["visual_style"],
                    "scene_beat": outline["beats"][index], "image_prompt": scene["image_prompt"],
                    "earlier_visuals": [s["image_prompt"] for s in script["scenes"][:index]]},
                    'Return ONLY JSON {"image_prompt":"..."}. Resolve the vague collection/item references '
                    "using the supplied visual context. Explicitly name and describe every physical subject "
                    "needed in this scene, including each product in a collection. Make a self-contained "
                    "still photograph description with composition, environment and lighting. Do not introduce "
                    "unrelated products. Omit camera movement, written copy, slogans, signs, offers, prices "
                    "and negative instructions. Do not say all three items or coordinated collection scene.",
                    check_grounded, f"grounded_{scene['id']}")
                save(script_path, script)
                event("visual_references_grounded", scene_id=scene["id"])
            try:
                validate_image_prompt(scene["image_prompt"])
            except ValueError:
                def check_visual(data):
                    return validate_image_prompt(data.get("image_prompt") if isinstance(data, dict) else None)
                seed = physical_visual_seed(scene["image_prompt"])
                try:
                    scene["image_prompt"] = validate_image_prompt(seed)
                except ValueError:
                    visual_context = {"subject": facts["subject"], "structured_facts": facts, "scene_beat": outline["beats"][index], "physical_visual_seed": seed,
                                      "style": outline["visual_style"], "scene_number": index+1}
                    scene["image_prompt"] = ask(visual_context,
                        'Return ONLY JSON {"image_prompt":"..."}. Write a detailed still photograph or '
                        "illustration description for the supplied physical subject and seed. Describe actual "
                        "objects, appearance, environment, lighting and composition only. Choose a distinct "
                        "shot appropriate to the scene number. Leave out slogans, written words, labels, "
                        "signs, prices and offers. Do not append negative instructions; give a positive "
                        "description containing only the physical visual content to generate.",
                        check_visual, f"visual_{scene['id']}")
                save(script_path, script)
                event("visual_prompt_repaired", scene_id=scene["id"])
            try:
                validate_spoken_narration(scene["narration"])
            except ValueError:
                def check_spoken(data):
                    text = validate_spoken_narration(data.get("narration") if isinstance(data, dict) else None)
                    return validate_batch({"scenes": [{**scene, "narration": text}]}, [slots[index]], index)[0]["narration"]
                scene["narration"] = ask(context,
                    f'Return ONLY JSON {{"narration":"..."}}. Write approximately {target_words(slots[index])} '
                    "words of natural voiceover for this scene and the user's requested video type. "
                    "Do not narrate camera moves, shot descriptions, captions or instructions to a designer. "
                    "Use only supplied factual claims and offers. Preserve story continuity when this is fiction.",
                    check_spoken, f"spoken_{scene['id']}")
                scene["tts_speed"] = 1.0
                save(script_path, script)
                event("spoken_copy_repaired", scene_id=scene["id"])

        initial_review = review_script("script_quality")
        if script_only:
            save(root / "manifest.json", {"job_id": job_id, "stage": "script_quality", "status": "script_ready"})
            return script_bundle(initial_review)

        stage_to("narration")
        audio = []
        for index, scene in enumerate(script["scenes"]):
            scene_to(scene["id"])
            slot = slots[index]
            fitted = False
            for attempt in range(1, 6):
                speed = scene.get("tts_speed", 1.0)
                path, seconds = synthesize(scene["narration"], scene["id"], speed)
                if facts["video_type"] in ("advertisement", "campaign") and index == max(0, len(slots)-2) and offer_copy(facts):
                    fitted = True
                    event("locked_offer_audio_preserved", seconds=round(seconds, 3))
                    break
                if narration_fits(seconds, slot):
                    fitted = True
                    break
                # Kokoro's speed parameter is not a linear duration control.
                # A small remote, pitch-preserving tempo adjustment is predictable
                # and avoids generating the same narration repeatedly on the GPU.
                tempo = round(seconds / (.94 * slot), 4)
                if .85 <= tempo <= 1.15:
                    adjusted = path.with_name(f"{path.stem}-tempo-{tempo}.wav")
                    if not adjusted.exists():
                        run(["ffmpeg", "-v", "error", "-y", "-i", str(path),
                             "-af", f"atempo={tempo}", "-c:a", "pcm_s16le", str(adjusted)])
                        save(adjusted.with_suffix(".json"), {"source": path.name, "tempo": tempo})
                    measured = wav_seconds(adjusted)
                    if narration_fits(measured, slot):
                        path, seconds, fitted = adjusted, measured, True
                        scene["audio_tempo"] = tempo
                        event("speech_tempo_fitted", scene_id=scene["id"], tempo=tempo, seconds=round(seconds, 3))
                        break
                if attempt == 5:
                    break
                desired_words = retiming_word_target(scene["narration"], seconds, slot)
                desired_words = min(math.ceil(target_words(slot)*1.5), max(math.floor(target_words(slot)*.6), desired_words))
                context = {"brief": brief, "structured_facts": facts, "whole_script": script["scenes"], "scene": scene, "scene_beat": outline["beats"][index],
                           "measured_audio_seconds": seconds, "desired_words": desired_words}

                def check_repair(data):
                    text = data.get("narration") if isinstance(data, dict) else None
                    if not isinstance(text, str):
                        raise ValueError("Return a narration string.")
                    if facts["video_type"] in ("advertisement", "campaign") and index == max(0, len(slots)-2) and offer_copy(facts):
                        text = offer_copy(facts)
                    # Actual speech duration is authoritative. A close word-count
                    # estimate must not discard usable model text before measuring it.
                    validate_spoken_narration(text)
                    candidate = {**script, "scenes": [{**s, "narration": text} if s["id"] == scene["id"] else s for s in script["scenes"]]}
                    problems = deterministic_issues(candidate, facts, brief)
                    if problems:
                        raise ValueError(f"Timing repair violates whole-script constraints: {problems}")
                    return validate_batch({"scenes": [{**scene, "narration": text}]}, [slot], index)[0]["narration"]

                if seconds < .88 * slot:
                    extra_words = max(3, desired_words-len(scene["narration"].split()))
                    original = scene["narration"]

                    def check_extension(data):
                        addition = data.get("additional_narration") if isinstance(data, dict) else None
                        text = extend_narration(original, addition)
                        check_repair({"narration": text})
                        return validate_batch({"scenes": [{**scene, "narration": text}]}, [slot], index)[0]["narration"]

                    try:
                        scene["narration"] = ask(context,
                        f'Return ONLY JSON {{"additional_narration":"..."}}. Write ONE additional spoken '
                        f"sentence or clause of approximately {extra_words} words to follow the existing narration. "
                        "Do not return or repeat the existing sentence. Add relevant meaning about the same scene, "
                        "using only the supplied facts/offer or the established fictional story. "
                        "Do not invent product benefits, new offers, camera directions or filler.",
                        check_extension, f"extend_{scene['id']}_{attempt}")
                    except Exception as exc:
                        event("timing_repair_kept_best_audio", error=str(exc))
                        break
                else:
                    try:
                        scene["narration"] = ask(context,
                        f'Return ONLY JSON {{"narration":"..."}}. Aim for {desired_words} spoken words in this scene. '
                        "Shorten the spoken copy while preserving essential meaning, user-supplied facts/offer "
                        "and narrative continuity. Return natural spoken text only, without stage directions.",
                        check_repair, f"timing_{scene['id']}_{attempt}")
                    except Exception as exc:
                        event("timing_repair_kept_best_audio", error=str(exc))
                        break
                scene["tts_speed"] = 1.0
                save(script_path, script)
                event("narration_rewritten", scene_id=scene["id"], words=len(scene["narration"].split()))
            if not fitted:
                event("adaptive_scene_timing_required", seconds=round(seconds, 3))
            scene.update(audio_seconds=seconds, audio_file=str(path.relative_to(root)))
            audio.append((path, seconds))
            save(script_path, script)
        durations = [item[1] for item in audio]
        # Fit the COMPLETE audio globally instead of forcing identical scene
        # lengths or dropping source facts to meet an arbitrary per-scene slot.
        total_speech = sum(durations)
        tempo = round(total_speech/(.94*brief["duration"]), 4)
        if not .88*brief["duration"] <= total_speech <= brief["duration"]-.08*len(audio):
            tempo = max(.85, tempo)
            if tempo > 1.25:
                raise RuntimeError("Narration is too dense for the requested runtime even after bounded timing repair; refusing truncation.")
            fitted_audio = []
            for scene, (path, seconds) in zip(script["scenes"], audio):
                adjusted = path.with_name(f"{path.stem}-global-tempo-{tempo}.wav")
                if not adjusted.exists():
                    run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-af", f"atempo={tempo}", "-c:a", "pcm_s16le", str(adjusted)])
                measured = wav_seconds(adjusted)
                scene.update(audio_seconds=measured, audio_file=str(adjusted.relative_to(root)), global_audio_tempo=tempo)
                fitted_audio.append((adjusted, measured))
            audio = fitted_audio
            durations = [item[1] for item in audio]
            save(root / "script.json", script)
            event("global_audio_timing_fitted", tempo=tempo)
        actual_slots = measured_scene_slots(durations, brief["duration"])
        for scene, actual_slot in zip(script["scenes"], actual_slots):
            scene["duration"] = actual_slot
        save(root / "script.json", script)
        event("measured_timeline_allocated", durations=actual_slots)
        final_review = review_script("final_script_quality", repair=False)
        save(root / "timeline.json", script)

        stage_to("images")
        pictures = []
        for scene in script["scenes"]:
            scene_to(scene["id"])
            path = root / "images" / f"{scene['id']}.png"
            meta = path.with_suffix(".json")
            prompt_hash = hashlib.sha256(scene["image_prompt"].encode()).hexdigest()
            if path.exists() and meta.exists() and json.loads(meta.read_text()).get("prompt_sha256") == prompt_hash:
                try:
                    with Image.open(path) as image:
                        image.verify()
                    event("image_reused", scene_id=scene["id"])
                    pictures.append(path)
                    continue
                except Exception:
                    event("invalid_cached_image", scene_id=scene["id"])
            for attempt in range(1, 4):
                active["attempt"] = attempt
                try:
                    begin = time.monotonic()
                    data = image_worker.generate.remote(scene["image_prompt"], 1280, 720)
                    with Image.open(io.BytesIO(data)) as image:
                        image.verify()
                    temp = path.with_suffix(".png.tmp")
                    temp.write_bytes(data)
                    temp.replace(path)
                    save(meta, {"prompt": scene["image_prompt"], "prompt_sha256": prompt_hash,
                                "generation_seconds": round(time.monotonic()-begin, 3)})
                    event("image_completed", scene_id=scene["id"], attempt=attempt)
                    break
                except Exception as exc:
                    event("image_attempt_failed", scene_id=scene["id"], attempt=attempt, error=str(exc))
                    if attempt == 3:
                        raise
                    time.sleep(attempt)
            pictures.append(path)

        stage_to("render")
        clips = []
        label = brief.get("label") or outline["label"]
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        for scene, picture in zip(script["scenes"], pictures):
            scene_to(scene["id"])
            clip = root / "clips" / f"{scene['id']}.mp4"
            meta = clip.with_suffix(".json")
            frames = round(scene["duration"] * FPS)
            signature = hashlib.sha256(json.dumps({"scene": scene, "label": label,
                "image": hashlib.sha256(picture.read_bytes()).hexdigest(), "render_revision": RENDER_REVISION}, sort_keys=True).encode()).hexdigest()
            if clip.exists() and meta.exists() and json.loads(meta.read_text()).get("signature") == signature:
                try:
                    info = json.loads(run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "json", str(clip)]))
                    if int(info["streams"][0]["nb_read_frames"]) == frames:
                        clips.append(clip)
                        event("clip_reused", scene_id=scene["id"])
                        continue
                except (RuntimeError, KeyError, ValueError, IndexError):
                    event("invalid_cached_clip", scene_id=scene["id"])
            overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            font_size = 26
            while font_size > 14:
                font = ImageFont.truetype(font_path, font_size)
                if draw.textlength(label, font=font) <= 760:
                    break
                font_size -= 1
            width = min(850, max(200, int(draw.textlength(label, font=font))+45))
            draw.rounded_rectangle((35, 25, 35+width, 76), radius=12, fill=(8, 32, 77, 210))
            draw.text((55, 37), label, font=font, fill="white")
            overlay_path = clip.with_suffix(".png")
            overlay.save(overlay_path)
            fade = min(.16, scene["duration"]/8)
            filtering = (f"[0:v]{motion_filter(frames, scene['motion'])}[bg];"
                         "[1:v]format=rgba[top];[bg][top]overlay=shortest=1,"
                         f"fade=t=in:st=0:d={fade},fade=t=out:st={scene['duration']-fade}:d={fade},format=yuv420p[out]")
            run(["ffmpeg", "-v", "error", "-y", "-i", str(picture), "-loop", "1", "-framerate", str(FPS),
                 "-i", str(overlay_path), "-filter_complex_threads", "1", "-filter_complex", filtering,
                 "-map", "[out]", "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "fast",
                 "-crf", "18", "-threads", "4", "-pix_fmt", "yuv420p", str(clip)])
            save(meta, {"signature": signature})
            event("clip_completed", scene_id=scene["id"])
            clips.append(clip)

        stage_to("assembly")
        joined_audio = root / "narration.wav"
        with wave.open(str(joined_audio), "wb") as joined:
            joined.setnchannels(1)
            joined.setsampwidth(2)
            joined.setframerate(24000)
            for scene, (path, _) in zip(script["scenes"], audio):
                with wave.open(str(path), "rb") as wav:
                    count, pcm = wav.getnframes(), wav.readframes(wav.getnframes())
                expected = round(scene["duration"]*24000)
                if count > expected:
                    raise RuntimeError("Refusing to truncate narration.")
                joined.writeframes(pcm + b"\0\0" * (expected-count))
        concat = root / "clips" / "concat.txt"
        concat.write_text("\n".join(f"file '{path}'" for path in clips), encoding="utf-8")
        # Linear-memory concat; do not decode all scenes in one massive xfade graph.
        run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-i", str(joined_audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)])
        stage_to("validation")
        probe = json.loads(run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,nb_read_frames,width,height,avg_frame_rate", "-of", "json", str(final)]))
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        if abs(float(probe["format"]["duration"])-brief["duration"]) > .045 or int(video["nb_read_frames"]) != round(brief["duration"]*FPS):
            raise RuntimeError("Output frame count/duration does not match the requested duration.")
        if not any(s["codec_type"] == "audio" for s in probe["streams"]):
            raise RuntimeError("Final video has no audio.")
        run(["ffmpeg", "-v", "error", "-i", str(final), "-f", "null", "-"])
        report = {"status": "completed", "job_id": job_id, "render_revision": RENDER_REVISION,
            "quality_revision": QUALITY_REVISION, "reviewed_script_sha256": final_review["reviewed_script_sha256"],
            "quality_status": "passed_with_advisories" if initial_review.get("warnings") or final_review.get("warnings") else "passed",
            "review_warnings": initial_review.get("warnings", []) + final_review.get("warnings", []),
            "verified_calculations": verified_offer(facts),
            "title": outline["title"], "prompt": brief["prompt"], "requested_duration_seconds": brief["duration"],
            "scene_count": len(slots), "unique_generated_images": len(pictures), "execution_location": "Modal",
            "probe": probe, "copy_source": "Qwen copy with generic source-bound commercial clauses and measured timing repair",
            "models": {"script": "Qwen2.5-7B-Instruct/A10G", "images": "FLUX.1-schnell/A100", "tts": "Kokoro-82M/T4"},
            "narration_seconds": round(sum(durations), 3), "padding_seconds": round(brief["duration"]-sum(durations), 3),
            "narration_coverage_percent": round(100*sum(durations)/brief["duration"], 2),
            "timing_warning": "Speech coverage is below 88%; best available complete narration preserved." if sum(durations)<.88*brief["duration"] else None,
            "maximum_added_scene_padding": round(max(s["duration"]-s["audio_seconds"] for s in script["scenes"]), 3),
            "transition": "short fades; linear-memory scene concatenation", "human_review_required": True,
            "limitations": ["Animated still images, not native neural video", "Generated identities/packaging are not guaranteed accurate", "Claims and offers require review", "No background music"],
            "elapsed_this_attempt_seconds": round(time.monotonic()-started, 3),
            "video_sha256": hashlib.sha256(final.read_bytes()).hexdigest()}
        event("completed")
        save(root / "script.json", script)
        save(root / "report.json", report)
        save(root / "manifest.json", {"job_id": job_id, "status": "completed", "stage": "completed", "video": "final.mp4"})
        return bundle(report)
    except Exception as exc:
        event("failed", error=str(exc))
        save(root / "failure.json", failure_record(job_id, stage, active, exc, traceback.format_exc()))
        save(root / "manifest.json", {"job_id": job_id, "status": "failed", "stage": stage, **active})
        raise
