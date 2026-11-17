import json
import unittest
from unittest.mock import patch
from pathlib import Path

from generate_video import build_parser, main
from video_plan import (parse_duration, resolve_duration, make_brief, scene_slots,
                        target_words, validate_batch, validate_outline, allocate_timeline,
                        job_hash, parse_json, narration_fits, retiming_word_target,
                        validate_image_prompt, validate_spoken_narration, physical_visual_seed, visual_topic,
                        extend_narration, content_is_valid, needs_visual_grounding)


class TestVideoPlan(unittest.TestCase):
    def test_default_and_explicit_duration(self):
        self.assertEqual(make_brief("A robot on Mars")["duration"], 30)
        for text, seconds in (("30s", 30), ("2 minutes", 120), ("1m 30s", 90), ("1m30s", 90), (300, 300), ("1.04s", 1.04)):
            self.assertEqual(parse_duration(text), seconds)
        self.assertEqual(resolve_duration("Create a 45-second video"), 45)
        self.assertEqual(resolve_duration("A 2 minute story"), 120)
        self.assertEqual(resolve_duration("Make a 1m30s documentary"), 90)
        self.assertEqual(resolve_duration("A 5 minute story", "30"), 30)

    def test_limits_and_invalid_inputs(self):
        for value in (0, -1, 301, "6 minutes", "nan", "1.01s", "abc"):
            with self.assertRaises(ValueError):
                parse_duration(value)
        with self.assertRaises(ValueError):
            make_brief("  ")
        with self.assertRaises(ValueError):
            resolve_duration("A 30 second clip about a 2 minute race")

    def test_frame_exact_dynamic_scenes(self):
        for duration in range(1, 301):
            slots = scene_slots(duration)
            self.assertAlmostEqual(sum(slots), duration)
            self.assertTrue(all(abs(s*25-round(s*25)) < .00001 for s in slots))
        self.assertEqual(len(scene_slots(30)), 4)
        self.assertEqual(len(scene_slots(300)), 38)

    def test_five_minute_chapters_merge_without_fixed_scene_numbering(self):
        slots = scene_slots(300)
        scenes, sizes = [], []
        for start in range(0, len(slots), 6):
            batch_slots = slots[start:start+6]
            sizes.append(len(batch_slots))
            data = {"scenes": [{"narration": " ".join(["word"]*target_words(slot)),
                                "image_prompt": f"Distinct landscape composition for scene {start+i+1} with a silver mountain.",
                                "motion": "slow zoom in"} for i, slot in enumerate(batch_slots)]}
            scenes.extend(validate_batch(data, batch_slots, start))
        self.assertEqual(sizes, [6, 6, 6, 6, 6, 6, 2])
        self.assertEqual(len({s["id"] for s in scenes}), 38)
        self.assertEqual(scenes[-1]["id"], "scene_038")
        self.assertAlmostEqual(sum(s["duration"] for s in scenes), 300)

    def test_topic_and_offer_not_replaced(self):
        campaign = "Summer running shoe sale: 20% off through June 30"
        self.assertEqual(make_brief(campaign)["prompt"], campaign)
        story = make_brief("The mystery of a lost moon", 60)
        self.assertNotEqual(job_hash(story), job_hash(make_brief(campaign, 60)))

    def test_batch_validation_has_no_product_or_image_reuse_map(self):
        slot = scene_slots(30)[0]
        text = " ".join(["word"]*target_words(slot))
        scene = {"narration": text, "image_prompt": "A detailed wide shot of a mysterious moon and its silver mountain range.", "motion": "pan up"}
        result = validate_batch({"scenes": [scene]}, [slot], 6)
        self.assertEqual(result[0]["id"], "scene_007")
        self.assertEqual(result[0]["image_prompt"], scene["image_prompt"])
        for wrong in ({"scenes": []}, {"scenes": [{**scene, "motion": "rotate"}]}, {"scenes": [{**scene, "narration": "Hi"}]}):
            with self.assertRaises(ValueError):
                validate_batch(wrong, [slot])
        draft = validate_batch({"scenes": [{**scene, "narration": "A moon appears."}]}, [slot], draft=True)
        self.assertEqual(draft[0]["narration"], "A moon appears.")

    def test_outline_validation(self):
        data = {"title": "Moon Mystery", "label": "Moon Mystery", "visual_style": "Silver and black, a recurring silver spacecraft", "beats": ["Arrival", "Discovery"]}
        self.assertEqual(validate_outline(data, 2)["title"], "Moon Mystery")
        with self.assertRaises(ValueError):
            validate_outline(data, 3)

    def test_measured_narration_budget(self):
        for duration in (1, 30, 45, 60, 180, 300):
            slots = scene_slots(duration)
            self.assertEqual(allocate_timeline([s*.94 for s in slots], duration), slots)
            with self.assertRaises(ValueError):
                allocate_timeline([s*.4 for s in slots], duration)
            with self.assertRaises(ValueError):
                allocate_timeline([s*1.1 for s in slots], duration)
        self.assertFalse(narration_fits(float("nan"), 5))
        self.assertEqual(retiming_word_target("one two three four", 2, 4), 8)

    def test_public_command_no_mock_or_missing_prompt(self):
        self.assertEqual(build_parser().parse_args(["Lost moon"]).provider, "modal")
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["Lost moon", "--provider", "mock"])
            with self.assertRaises(SystemExit):
                main(["Lost moon", "--duration", "301"])
            with self.assertRaises(SystemExit):
                build_parser().parse_args([])

    def test_json_and_content_presets_removed(self):
        self.assertEqual(parse_json('```json\n{"ok":true}\n```'), {"ok": True})
        for file in ("generate_video.py", "web/jobs.py", "modal_workers/video.py"):
            source = Path(file).read_text(encoding="utf-8")
            self.assertNotIn("NIVEA", source)
            self.assertNotIn("deep_frontier", source)

    def test_visual_and_spoken_direction_guards(self):
        for prompt in ("Running shoes with text '20% Off' on the right side of the photograph.",
                       "A clerk holding a discounted sign beside a rack of colorful running shoes.",
                       "Photograph of running shoes with no text or lettering in the image."):
            with self.assertRaises(ValueError):
                validate_image_prompt(prompt)
        validate_image_prompt("Colorful running shoes on a simple display stand with soft studio light.")
        validate_spoken_narration("Save 20% on running shoes through June 30. Find your next pair today.")
        with self.assertRaises(ValueError):
            validate_spoken_narration("Close-up of shoes. Pan up to skyline. Text: Shop Now.")
        with self.assertRaises(ValueError):
            validate_spoken_narration("Text: 'Shop now'.")
        original = "Running shoes sit on a wooden rack under soft light. The text '20% Off' is printed on the right."
        self.assertEqual(physical_visual_seed(original), "Running shoes sit on a wooden rack under soft light.")
        self.assertEqual(visual_topic("20% Off Running Shoes"), "Running Shoes")
        self.assertEqual(extend_narration("A light appears.", "The explorer follows it."),
                         "A light appears. The explorer follows it.")
        with self.assertRaises(ValueError):
            extend_narration("A light appears.", "A light appears.")
        for prompt in ("The sign reads 'Limited Time Sale' and is positioned beside the shoes.",
                       "Hurry!' appears on the right side of the frame beside running shoes."):
            with self.assertRaises(ValueError):
                validate_image_prompt(prompt)
        self.assertFalse(content_is_valid({"scenes": []}))
        self.assertTrue(needs_visual_grounding("A coordinated collection scene showing all three items together."))
        self.assertFalse(needs_visual_grounding("Sunglasses, running shoes and a woven beach bag arranged on sand."))
        self.assertFalse(content_is_valid({"scenes": [{"image_prompt": "A coordinated collection scene showing all three items together.", "narration": "Explore our summer collection today."}]}))


if __name__ == "__main__":
    unittest.main()
