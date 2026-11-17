"""Prompt-driven video contracts and timing math; no local media work."""
import hashlib
import json
import math
import re

VERSION = 2
RENDER_REVISION = 2
FPS = 25
MAX_DURATION = 300
MOTIONS = ("pan right", "pan left", "slow zoom in", "pan up", "slow zoom out", "static", "pan down")
VISUAL_COPY = re.compile(r"\b(?:text|lettering|caption|overlay|font|countdown|price tag|discount(?:ed)? sign|sale sign|sign\b[^.!?]{0,50}\b(?:reads|says|shows)|(?:appears|displayed) (?:on|at) (?:the )?(?:left|right|bottom|top))\b|[%$£€]", re.I)


def physical_visual_seed(prompt):
    """Keep model-written physical sentences; remove unsafe copy instructions."""
    return " ".join(sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", prompt)
                    if not VISUAL_COPY.search(sentence)).strip()


def visual_topic(label):
    return re.sub(r"\d+(?:\.\d+)?\s*%\s*(?:off)?", "", label, flags=re.I).strip()


def validate_image_prompt(prompt):
    if not isinstance(prompt, str) or len(prompt.strip()) < 30:
        raise ValueError("A detailed image prompt is required.")
    if VISUAL_COPY.search(prompt):
        raise ValueError("Describe physical visuals only: omit printed copy, prices, discounts and text instructions, including negative text instructions.")
    return prompt.strip()


def needs_visual_grounding(prompt):
    """An image worker has no earlier-scene context for unresolved references."""
    return bool(re.search(r"\b(?:all (?:three|four|these|the) items|(?:these|those|the aforementioned) (?:items|products)|coordinated collection scene)\b", prompt, re.I))


def validate_spoken_narration(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Narration must be nonempty spoken text.")
    if re.search(r"\btext\s*:|\b(?:text overlay|close[- ]up|pan (?:up|down|left|right)|zoom (?:in|out)|focus on|camera (?:cuts?|zooms?|pans?|moves?|shows?)|(?:wide|final) shot|scene transitions)\b", text, re.I):
        raise ValueError("Return spoken narration, not camera directions or image/overlay instructions.")
    return text.strip()


def extend_narration(original, addition):
    addition = validate_spoken_narration(addition)
    if addition.casefold() in original.casefold():
        raise ValueError("Add new relevant spoken content, not a repeat of the existing narration.")
    return validate_spoken_narration(original.rstrip() + " " + addition)


def content_is_valid(script):
    try:
        scenes = script["scenes"]
        return bool(scenes) and all(validate_image_prompt(s["image_prompt"]) and
                                    not needs_visual_grounding(s["image_prompt"]) and
                                    validate_spoken_narration(s["narration"]) for s in scenes)
    except (KeyError, TypeError, ValueError):
        return False


def parse_duration(value):
    text = str(value).strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        seconds = float(text)
    else:
        pattern = r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|seconds?|secs?|s)(?=\d|\b)"
        parts = re.findall(pattern, text)
        if not parts or re.sub(pattern, "", text).strip():
            raise ValueError("Use seconds, '30s', '2 minutes', or '1m 30s'.")
        seconds = sum(float(n) * (60 if unit.startswith("m") else 1) for n, unit in parts)
    if not math.isfinite(seconds) or not 1 <= seconds <= MAX_DURATION:
        raise ValueError("Duration must be between 1 second and 5 minutes (300 seconds).")
    if abs(seconds * FPS - round(seconds * FPS)) > .000001:
        raise ValueError("Duration must align to 25 FPS (multiples of 0.04 seconds).")
    return int(seconds) if seconds.is_integer() else seconds


def resolve_duration(prompt, explicit=None):
    if explicit is not None:
        return parse_duration(explicit)
    number = r"\d+(?:\.\d+)?"
    matches = re.findall(rf"(?<![\w.])({number}[\s-]*(?:minutes?|mins?|m)(?:\s*{number}\s*(?:seconds?|secs?|s))?|{number}[\s-]*(?:seconds?|secs?|s))\b", prompt, re.I)
    if not matches:
        return 30
    durations = {parse_duration(value.replace("-", " ")) for value in matches}
    if len(durations) != 1:
        raise ValueError("Multiple times appear in the prompt. Set --duration explicitly.")
    return durations.pop()


def make_brief(prompt, duration=None, voice="af_heart", details="", label=None):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A nonempty title or prompt is required.")
    if label is not None and (not isinstance(label, str) or not 1 <= len(label.strip()) <= 55):
        raise ValueError("Label must contain 1–55 characters.")
    return {"prompt": prompt.strip(), "duration": resolve_duration(prompt, duration),
            "voice": voice, "details": details or "", "label": label}


def job_hash(brief):
    return hashlib.sha256(json.dumps({"version": VERSION, **brief}, sort_keys=True).encode()).hexdigest()


def parse_json(text):
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))


def scene_slots(duration):
    duration = parse_duration(duration)
    count = max(1, math.ceil(duration / 8))
    total = round(duration * FPS)
    return [(total // count + (i < total % count)) / FPS for i in range(count)]


def target_words(seconds):
    return max(1, round(seconds * .94 * 2.8))


def validate_outline(data, count):
    if not isinstance(data, dict):
        raise ValueError("Outline must be an object.")
    title, style, beats = data.get("title"), data.get("visual_style"), data.get("beats")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 150:
        raise ValueError("Outline needs a short title.")
    if not isinstance(style, str) or not style.strip():
        raise ValueError("Outline needs a shared visual style and continuity description.")
    if not isinstance(beats, list) or len(beats) != count or any(not isinstance(b, str) or not b.strip() for b in beats):
        raise ValueError(f"Outline requires exactly {count} distinct scene beats.")
    label = data.get("label", title)
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 55:
        raise ValueError("Label must contain 1–55 characters.")
    return {"title": title.strip(), "label": label.strip(), "visual_style": style.strip(), "beats": beats}


def validate_batch(data, slots, start=0, draft=False):
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list) or len(data["scenes"]) != len(slots):
        raise ValueError(f"Return exactly {len(slots)} scenes.")
    scenes = []
    for i, (scene, slot) in enumerate(zip(data["scenes"], slots)):
        if not isinstance(scene, dict):
            raise ValueError("Scene must be an object.")
        text, prompt, motion = scene.get("narration"), scene.get("image_prompt"), scene.get("motion")
        words = target_words(slot)
        low, high = max(1, math.floor(words * .6)), max(4, math.ceil(words * 1.5))
        if draft:
            low, high = 1, max(60, high)
        if not isinstance(text, str) or not low <= len(text.split()) <= high:
            count = len(text.split()) if isinstance(text, str) else "invalid"
            raise ValueError(f"Scene {start+i+1} has {count} words; requires {low}–{high}, targeting {words}.")
        if not isinstance(prompt, str) or len(prompt.strip()) < 30:
            raise ValueError("Every scene requires a detailed image_prompt.")
        if motion not in MOTIONS:
            raise ValueError(f"motion must be one of {MOTIONS}.")
        scenes.append({"id": f"scene_{start+i+1:03d}", "narration": text.strip(),
                       "image_prompt": prompt.strip(), "motion": motion, "duration": slot})
    return scenes


def narration_fits(seconds, slot):
    return math.isfinite(seconds) and seconds > 0 and .88 * slot <= seconds <= slot - min(.08, .04 * slot)


def retiming_word_target(text, seconds, slot):
    if seconds <= 0:
        raise ValueError("Cannot calibrate empty audio.")
    return max(1, round(len(text.split()) * (.94 * slot) / seconds))


def allocate_timeline(audio_seconds, target=30, fps=FPS):
    slots = scene_slots(target)
    if len(audio_seconds) != len(slots):
        raise ValueError("Audio count must match duration-derived scene count.")
    for i, (seconds, slot) in enumerate(zip(audio_seconds, slots)):
        if not narration_fits(seconds, slot):
            raise ValueError(f"Scene {i+1}: {seconds:.3f}s speech does not fit {slot:.3f}s naturally.")
    return slots


def measured_scene_slots(audio_seconds, target=30, fps=FPS):
    """Exact runtime, allocating visual time around complete measured speech."""
    if not audio_seconds or any(not math.isfinite(s) or s <= 0 for s in audio_seconds):
        raise ValueError("Every scene needs nonempty measured audio.")
    total = round(parse_duration(target) * fps)
    frames = [math.ceil(s*fps)+2 for s in audio_seconds]
    spare = total-sum(frames)
    if spare < 0:
        raise ValueError("Complete narration exceeds the runtime; tempo or copy repair is required.")
    weights = [s/sum(audio_seconds) for s in audio_seconds]
    extras = [math.floor(spare*w) for w in weights]
    remainder = spare-sum(extras)
    order = sorted(range(len(extras)), key=lambda i: spare*weights[i]-extras[i], reverse=True)
    for index in order[:remainder]:
        extras[index] += 1
    return [(f+e)/fps for f, e in zip(frames, extras)]
