"""Reglas de YOLO y fuego/humo con detecciones sinteticas (no necesitan PyTorch ni video)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import detector  # noqa: E402


def box(c, x, y, w, h, conf=0.9):
    return {"c": c, "x1": x, "y1": y, "x2": x + w, "y2": y + h, "conf": conf}


def whys(samples, zones=None):
    return {t["why"] for t in detector.analyze(samples, zones)["triggers"]}


class Rules(unittest.TestCase):
    def test_classes_keep_front_indexes(self):
        self.assertEqual(detector.CLASSES[:9], ["person", "bicycle", "car", "motorcycle", "bus", "truck", "backpack", "handbag", "suitcase"])
        self.assertEqual(set(detector.COCO.values()), set(detector.CLASSES))

    def test_vehicle_contact_with_hard_brake(self):
        s = []
        for k in range(18):                                   # 3 muestras/s, 6 s
            t = k / 3
            x = min(0.05 + 0.15 * t, 0.42)                    # el auto 1 avanza y se detiene pegado al 2
            s.append((t, [box("car", x, 0.5, 0.12, 0.1), box("car", 0.5, 0.5, 0.12, 0.1)]))
        self.assertIn("yolo:contacto_vehiculos", whys(s))

    def test_two_cars_side_by_side_do_not_trigger_contact(self):
        s = [(k / 3, [box("car", 0.2 + 0.1 * (k / 3), 0.5, 0.12, 0.1), box("car", 0.5 + 0.1 * (k / 3), 0.5, 0.12, 0.1)]) for k in range(18)]
        self.assertNotIn("yolo:contacto_vehiculos", whys(s))

    def test_person_overlapping_vehicle_edge(self):
        s = [(k / 3, [box("bus", 0.30, 0.4, 0.4, 0.3), box("person", 0.66, 0.45, 0.07, 0.2)]) for k in range(6)]
        self.assertIn("yolo:persona_vehiculo", whys(s))

    def test_person_inside_car_or_on_motorbike_is_ignored(self):
        inside = [(k / 3, [box("car", 0.30, 0.4, 0.4, 0.3), box("person", 0.46, 0.45, 0.07, 0.15)]) for k in range(6)]
        moto = [(k / 3, [box("motorcycle", 0.30, 0.4, 0.1, 0.2), box("person", 0.31, 0.35, 0.06, 0.18)]) for k in range(6)]
        self.assertNotIn("yolo:persona_vehiculo", whys(inside))
        self.assertNotIn("yolo:persona_vehiculo", whys(moto))

    def test_animal_and_knife(self):
        self.assertIn("yolo:animal", whys([(k / 3, [box("dog", 0.4, 0.6, 0.1, 0.1)]) for k in range(6)]))
        s = [(k / 3, [box("person", 0.4, 0.3, 0.1, 0.4), box("knife", 0.47, 0.5, 0.03, 0.06)]) for k in range(6)]
        self.assertIn("yolo:objeto_peligroso", whys(s))
        alone = [(k / 3, [box("knife", 0.8, 0.8, 0.03, 0.06)]) for k in range(6)]
        self.assertNotIn("yolo:objeto_peligroso", whys(alone))

    def test_hint_warns_detector_is_blind_to_fire(self):
        h = detector.hint_for([[1.0, [[2, 0.1, 0.1, 0.2, 0.2, 1, ""]]]], [], 1.0)
        self.assertIn("NO reconoce fuego", h)
        self.assertIn("1 auto", h)

    def test_weights_resolve_to_repo_models_folder(self):
        detector.YOLO_WEIGHTS = "yolo11n.pt"
        self.assertTrue(detector.resolve_weights().endswith("models/yolo11n.pt"))


if __name__ == "__main__":
    unittest.main()
