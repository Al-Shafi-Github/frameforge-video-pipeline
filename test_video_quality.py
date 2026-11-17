import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from video_quality import (validate_facts, numbers, deterministic_issues,
    validate_review, script_fingerprint, quality_cache_valid, failure_record,
    QUALITY_REVISION, verified_offer, bind_commercial_copy, offer_copy)
from video_plan import make_brief, validate_image_prompt, measured_scene_slots


class TestVideoQuality(unittest.TestCase):
    def setUp(self):
        self.brief = make_brief("Promote black leather shoes, now 3000, previously 5000, offer lasts 20 days")
        self.raw = {"video_type": "advertisement", "subject": "black leather shoes", "facts": [
            {"field": "product", "value": "black leather shoes", "evidence": "black leather shoes"},
            {"field": "current_price", "value": "3000", "evidence": "now 3000"},
            {"field": "previous_price", "value": "5000", "evidence": "previously 5000"},
            {"field": "offer_deadline", "value": "20 days", "evidence": "20 days"}]}
        self.facts = validate_facts(self.raw, self.brief)
        texts = ["Meet your next style companion: black leather shoes for your next look.",
                 "Take a closer look at the black leather finish and distinctive silhouette.",
                 "Previously five thousand, now three thousand. This special offer lasts twenty days only.",
                 "Bring your look together. Explore this pair and choose your next outfit today."]
        self.script = {"scenes": [{"id": f"scene_{i+1:03d}", "narration": t,
            "image_prompt": "Black leather shoes on a wooden stand under warm studio lighting.",
            "motion": "static", "duration": 7.5} for i, t in enumerate(texts)]}

    def test_extraction_grounded_and_currency_unknown(self):
        self.assertEqual(self.facts["currency"], [])
        with self.assertRaises(ValueError):
            validate_facts({**self.raw, "facts": self.raw["facts"] + [
                {"field": "currency", "value": "USD", "evidence": "USD"}]}, self.brief)
        with self.assertRaises(ValueError):
            validate_facts({**self.raw, "facts": self.raw["facts"][:-1]}, self.brief)

    def test_general_product_and_story_not_shoe_preset(self):
        brief = make_brief("Promote red cotton shirts, now BDT 800, previously BDT 1000")
        data = {"video_type": "campaign", "subject": "red cotton shirts", "facts": [
            {"field": "product", "value": "red cotton shirts", "evidence": "red cotton shirts"},
            {"field": "current_price", "value": "BDT 800", "evidence": "now BDT 800"},
            {"field": "previous_price", "value": "BDT 1000", "evidence": "previously BDT 1000"}]}
        self.assertEqual(validate_facts(data, brief)["currency"], ["BDT"])
        fiction = {"video_type": "story", "facts": [], "subject": "a moon explorer"}
        self.assertFalse(deterministic_issues({"scenes": [{"id": "scene_001", "narration": "The explorer finds twenty moons.", "image_prompt": "An explorer surrounded by glowing moons in a deep blue star field."}]}, fiction, make_brief("A moon explorer")))

    def test_numbers_and_currency_survive_timing_repair(self):
        self.assertEqual(numbers("3,000 and five thousand; twenty days"), {3000, 5000, 20})
        self.assertFalse(deterministic_issues(self.script, self.facts, self.brief))
        self.script["scenes"][2]["narration"] = "Now $3000, previously $7000. Offer lasts thirty days."
        codes = {issue["code"] for issue in deterministic_issues(self.script, self.facts, self.brief)}
        self.assertTrue({"currency", "numbers", "missing_offer"}.issubset(codes))
        self.assertTrue(all(issue["scene_ids"] == ["scene_003"] for issue in deterministic_issues(self.script, self.facts, self.brief) if issue["code"] == "missing_offer"))

    def test_whole_script_repetition(self):
        for scene in self.script["scenes"]:
            scene["narration"] = "Buy black leather shoes now 3000, previously 5000, for 20 days."
        codes = {issue["code"] for issue in deterministic_issues(self.script, self.facts, self.brief)}
        self.assertTrue({"duplicate_copy", "repeated_offer", "repeated_phrase"}.issubset(codes))

    def test_reviews_and_cache_require_current_copy(self):
        with self.assertRaises(ValueError):
            validate_review({"issues": [{"scene_ids": ["scene_999"], "code": "bad", "message": "bad"}]}, self.script)
        report = {"quality_revision": QUALITY_REVISION, "reviewed_script_sha256": script_fingerprint(self.script)}
        self.assertTrue(quality_cache_valid(report, self.script))
        self.script["scenes"][0]["audio_seconds"] = 7
        self.assertTrue(quality_cache_valid(report, self.script))
        self.script["scenes"][0]["narration"] += " Changed copy."
        self.assertFalse(quality_cache_valid(report, self.script))
        self.assertFalse(quality_cache_valid({}, self.script))

    def test_failure_identifies_job_scene_request(self):
        record = failure_record("job-123", "narration", {"scene_id": "scene_003", "request": "timing_scene_003", "attempt": 3}, ValueError("bad copy"), "trace")
        self.assertEqual(record["scene_id"], "scene_003")
        self.assertEqual(record["job_id"], "job-123")
        self.assertEqual(record["attempt"], 3)
        for image in ("A shoe store sign above it that reads 'Offer' beside black shoes.",
                      "A countdown timer displays twenty days.", "Black shoes with clean gold font behind them."):
            with self.assertRaises(ValueError):
                validate_image_prompt(image)

    def test_verified_arithmetic_and_data_bound_offer(self):
        self.assertEqual(verified_offer(self.facts), {"savings": 2000, "discount_percent": 40})
        self.script["scenes"][2]["narration"] = "Now 3000, previously 5000, save 2000 or 40% off. Offer lasts 20 days."
        self.assertFalse(deterministic_issues(self.script, self.facts, self.brief))
        self.script["scenes"][2]["narration"] = "Now 2000, previously 5000, save 2000. Offer lasts 20 days."
        self.assertIn("changed_price", {i["code"] for i in deterministic_issues(self.script, self.facts, self.brief)})
        self.script["scenes"][0]["narration"] = "Premium black leather shoes. Now $7000, only 50% off."
        changed = bind_commercial_copy(self.script, self.facts, self.brief)
        self.assertIn("scene_001", changed)
        self.assertEqual(self.script["scenes"][2]["narration"], offer_copy(self.facts))
        self.assertFalse(deterministic_issues(self.script, self.facts, self.brief))
        self.assertNotIn("premium", self.script["scenes"][0]["narration"].lower())

    def test_measured_timeline_preserves_unequal_speech_and_exact_runtime(self):
        for durations in ([5, 6, 10, 7], [7.02, 7.1, 6.4, 7.05], [3, 4, 6, 5]):
            slots = measured_scene_slots(durations, 30)
            self.assertEqual(round(sum(slots)*25), 750)
            self.assertTrue(all(slot >= duration+.04 for duration, slot in zip(durations, slots)))
        with self.assertRaises(ValueError):
            measured_scene_slots([10, 10, 10, 10], 30)

    def test_no_implicit_cross_currency_discount(self):
        self.assertEqual(verified_offer({**self.facts, "currency": ["BDT", "USD"]}), {})

    def test_explicit_currency_survives_generic_binding(self):
        brief = make_brief("Promote black leather shoes, now 3000, previously 5000, offer lasts 20 days; currency BDT")
        facts = validate_facts(self.raw, brief)
        bound = offer_copy(facts)
        self.assertIn("BDT 3000", bound)
        self.assertIn("BDT 5000", bound)

    def test_orchestrator_resume_after_failed_chapter_without_model_compute(self):
        # Real orchestration/checkpoint code; fake remote model, no TTS/image/FFmpeg.
        from modal_workers import video
        from video_plan import job_hash
        with tempfile.TemporaryDirectory() as directory:
            real_path = Path
            mapped_path = lambda value: real_path(directory) if value == "/jobs" else real_path(value)
            fail = [True]
            fail_scene_repair = [False]
            calls = []
            def answer(context, instruction):
                calls.append(instruction)
                if "Extract the user's" in instruction:
                    return json.dumps(self.raw)
                if "keys title, label, visual_style and beats" in instruction:
                    return json.dumps({"title": "Leather Style", "label": "Leather Style", "visual_style": "Warm studio light on black leather shoes", "beats": ["Introduction", "Detail", "Offer", "Invitation"]})
                if 'Return ONLY JSON {"scenes"' in instruction:
                    if fail[0]:
                        raise RuntimeError("Injected chapter failure")
                    return json.dumps(self.script)
                if 'Return ONLY JSON {"narrations"' in instruction:
                    return json.dumps({"narrations": [s["narration"] for s in self.script["scenes"]]})
                if 'Return ONLY JSON {"issues"' in instruction:
                    scene_data = json.loads(context)["scenes"]
                    if "NEEDS_REPAIR" in scene_data[1]["narration"]:
                        return json.dumps({"issues": [{"scene_ids": ["scene_002"], "code": "weak_copy", "message": "Remove the injected weak-copy marker."}]})
                    return '{"issues":[]}'
                if "Repair only this scene" in instruction:
                    if fail_scene_repair[0]:
                        raise RuntimeError("Injected scene repair failure")
                    return json.dumps({"narration": json.loads(context)["current_narration"].replace(" NEEDS_REPAIR", "")})
                raise AssertionError("Unexpected model request: " + instruction)
            worker = Mock()
            worker.generate.remote.side_effect = answer
            with patch("builtins.print"), patch.dict("sys.modules", {"PIL": Mock()}), patch.object(video, "Path", side_effect=mapped_path), patch.object(video.volume, "commit"), patch("modal.Cls.from_name", return_value=lambda: worker):
                with self.assertRaises(RuntimeError):
                    video.generate.local(self.brief, script_only=True)
                root = real_path(directory) / job_hash(self.brief)
                failure = json.loads((root / "failure.json").read_text())
                self.assertIn("chapter_q", failure["request"])
                self.assertEqual(failure["attempt"], 3)
                before = len(calls)
                fail[0] = False
                result = video.generate.local(self.brief, script_only=True)
                self.assertEqual(result["report"]["status"], "script_ready")
                resumed = calls[before:]
                self.assertFalse(any("Extract the user's" in c or "keys title, label" in c for c in resumed))
                # Further identical resume reuses the reviewed copy as well.
                before = len(calls)
                video.generate.local(self.brief, script_only=True)
                self.assertEqual(len(calls), before)
                # Now fail a specific scene repair; untouched copy stays saved.
                saved = json.loads((root / "script.json").read_text())
                untouched = [saved["scenes"][i] for i in (0, 2, 3)]
                saved["scenes"][1]["narration"] += " NEEDS_REPAIR"
                (root / "script.json").write_text(json.dumps(saved))
                fail_scene_repair[0] = True
                video.generate.local(self.brief, script_only=True)
                review = json.loads((root / "script_quality.json").read_text())
                self.assertTrue(any(w["code"] == "kept_best_draft" for w in review["warnings"]))
                self.assertEqual(review["issues"], [])
                before = len(calls)
                fail_scene_repair[0] = False
                video.generate.local(self.brief, script_only=True)
                resumed = calls[before:]
                self.assertEqual(resumed, [])
                repaired = json.loads((root / "script.json").read_text())
                self.assertEqual([{k: s[k] for k in ("id", "narration", "image_prompt", "motion", "duration")}
                                  for s in [repaired["scenes"][i] for i in (0, 2, 3)]],
                                 [{k: s[k] for k in ("id", "narration", "image_prompt", "motion", "duration")} for s in untouched])


if __name__ == "__main__":
    unittest.main()
