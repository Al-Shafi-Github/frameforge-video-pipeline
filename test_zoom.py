import unittest

from render.zoom import zoom_at, zoom_filter


class TestZoom(unittest.TestCase):
    def test_endpoints_and_monotonic_motion_for_all_preview_lengths(self):
        for frames in (228, 213, 261, 243, 242, 313):
            for motion, endpoints in (("slow zoom in", (1, 1.1)), ("slow zoom out", (1.1, 1))):
                values = [zoom_at(i, frames, motion) for i in range(frames)]
                self.assertAlmostEqual(values[0], endpoints[0])
                self.assertAlmostEqual(values[-1], endpoints[1])
                direction = 1 if motion.endswith("in") else -1
                self.assertTrue(all(direction * (b-a) > 0 for a, b in zip(values, values[1:])))
                self.assertLess(abs(values[1]-values[0]), abs(values[frames//2]-values[frames//2-1]))

    def test_precision_format_precedes_zoom_and_playback_format_follows(self):
        expression = zoom_filter(228, "slow zoom in")
        self.assertLess(expression.index("format=yuv444p"), expression.index("zoompan"))
        self.assertTrue(expression.endswith("format=yuv420p"))
        self.assertIn("flags=lanczos,format=yuv444p,zoompan", expression)
        self.assertIn("fps=25,format=yuv444p,format=yuv420p", expression)
        self.assertIn("trunc(iw/zoom)", expression)
        self.assertIn("on/227", expression)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            zoom_filter(1, "slow zoom in")
        with self.assertRaises(ValueError):
            zoom_filter(228, "slow zoom in", 1234)


if __name__ == "__main__":
    unittest.main()
