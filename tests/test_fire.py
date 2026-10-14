"""Detector local de fuego y humo con escenas sinteticas."""
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fire  # noqa: E402

RNG = np.random.default_rng(1)


def bg():
    im = np.full((360, 640, 3), (90, 95, 100), np.uint8)
    cv2.randn(im, (90, 95, 100), (6, 6, 6))
    return im


def flame(k):
    im = bg()
    for _ in range(6):
        cv2.ellipse(im, (320 + int(RNG.normal(0, 18)), 240 + int(RNG.normal(0, 14))), (int(RNG.integers(14, 34)), int(RNG.integers(24, 60))),
                    0, 0, 360, (40, int(RNG.integers(120, 200)), 255), -1)
    cv2.circle(im, (320, 250), 10, (120, 230, 255), -1)
    return im


def cone(k):
    im = bg()
    cv2.rectangle(im, (300, 200), (360, 300), (30, 140, 255), -1)
    return im


def bus(k):
    im = bg()
    x = 20 + k * 18
    cv2.rectangle(im, (x, 150), (x + 200, 250), (30, 150, 255), -1)
    return im


def smoke(k):
    im = bg()
    mask = np.zeros(im.shape[:2], np.uint8)
    cv2.circle(mask, (320, 200), 20 + k * 9, 255, -1)
    a = (cv2.GaussianBlur(mask, (0, 0), 12).astype(np.float32) / 255 * 0.8)[..., None]
    im = (im * (1 - a) + np.full_like(im, 150) * a).astype(np.uint8)
    return np.clip(im.astype(np.int16) + RNG.integers(-10, 10, im.shape), 0, 255).astype(np.uint8)


def run(gen, n=28, fps=4):
    prev, sm = None, []
    for k in range(n):
        prev, m = fire.metrics(gen(k), prev)
        sm.append((k / fps, m))
    return {e["why"] for e in fire.analyze(sm)}


class Fire(unittest.TestCase):
    def test_flames_trigger(self):
        self.assertIn("fire:fuego", run(flame))

    def test_smoke_triggers(self):
        self.assertIn("fire:humo", run(smoke))

    def test_static_or_moving_orange_objects_do_not_trigger(self):
        self.assertEqual(run(cone), set())
        self.assertEqual(run(bus), set())

    def test_calm_scene(self):
        self.assertEqual(run(lambda k: bg()), set())


if __name__ == "__main__":
    unittest.main()
