"""Cola de OBS: varios videos en cadena, registro, reintento y retoma tras reinicio (OBS simulado con una carpeta local).
   python -m unittest discover -s tests -v"""
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402,F401  (fija el entorno antes de importar app)
_tmp, _obs = _env.TMP, _env.OBS
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as vialert  # noqa: E402


async def _fast_mock(idx, describe=False):
    ev = ["NORMAL_TRAFFIC", "COLLISION", "FIRE_OR_SMOKE", "NORMAL_TRAFFIC"][idx % 4]
    r = {"event": ev, "events": [ev], "person_down": False, "flags": [], "confidence": 0.9, "severity": "high" if ev != "NORMAL_TRAFFIC" else "low",
         "reason": "prueba", "scene": "escena de prueba"}
    return r, {"http_ms": 1, "tokens_in": 10, "tokens_out": 5, "tokens_total": 15, "cached": 0, "finish": "stop", "raw": "{}",
               "describe": describe, "retried": False}


vialert.call_mock = _fast_mock


def make_video(path: Path, secs=9, fps=10):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for k in range(secs * fps):
        im = np.full((240, 320, 3), 110, np.uint8)
        cv2.rectangle(im, (10 + k * 2, 100), (70 + k * 2, 150), (40, 40, 200), -1)
        vw.write(im)
    vw.release()


H = {"X-Vialert-Token": "tok-123"}


class Ingest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (_obs / "b1" / "incoming").mkdir(parents=True)
        for n in ("a", "b", "c"):
            make_video(_obs / "b1" / "incoming" / f"{n}.mp4")
        cls.cm = TestClient(vialert.app)
        cls.C = cls.cm.__enter__()          # dispara el startup: cola y trabajadores

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)

    def wait(self, ids, secs=90):
        t0 = time.time()
        while time.time() - t0 < secs:
            items = {i["id"]: i for i in self.C.get("/api/ingest/list").json()["items"]}
            if all(items[i]["status"] in ("done", "error") for i in ids):
                return items
            time.sleep(0.5)
        self.fail("la cola no termino: " + json.dumps(self.C.get("/api/ingest/list").json()["counts"]))

    def test_1_token(self):
        self.assertEqual(self.C.post("/api/ingest", json={"bucket": "b1", "key": "incoming/a.mp4"}).status_code, 401)

    def test_2_chain_of_videos(self):
        ids = []
        for n in ("a", "b", "c"):                                  # tres avisos seguidos, sin esperar
            r = self.C.post("/api/ingest", json={"bucket": "b1", "key": f"incoming/{n}.mp4"}, headers=H)
            self.assertEqual(r.status_code, 202)
            ids.append(r.json()["id"])
        dup = self.C.post("/api/ingest", json={"bucket": "b1", "key": "incoming/a.mp4"}, headers=H).json()
        self.assertTrue(dup.get("duplicate"))
        self.assertEqual(self.C.get("/api/ingest/list").json()["counts"]["queued"] + 0 >= 0, True)
        items = self.wait(ids)
        for i in ids:
            it = items[i]
            self.assertEqual(it["status"], "done", it)
            self.assertGreater(it["frames"], 0)
            self.assertTrue(it["incidents"])
            rep = _obs / "b1" / "vialert-results" / "incoming" / f"{'abc'[ids.index(i)]}.mp4" / "report.json"
            self.assertTrue(rep.exists(), rep)
        spans = sorted((items[i]["started"], items[i]["finished"]) for i in ids)
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            self.assertLessEqual(e1, s2 + 0.5)                      # 1 trabajador: uno tras otro, sin pisarse
        led = json.loads((Path(_tmp) / "_ingest.json").read_text())
        self.assertEqual(len([x for x in led if x["status"] == "done"]), 3)

    def test_3_history_has_source_and_time(self):
        d = self.C.get("/api/logs").json()
        self.assertIn("obs", d["cams"])
        obs = [i for i in d["incidents"] if i["kind"] == "obs"]
        self.assertTrue(obs)
        srcs = {i["source"] for i in obs}
        self.assertTrue({"b1/incoming/a.mp4", "b1/incoming/b.mp4", "b1/incoming/c.mp4"} <= srcs, srcs)   # no se mezclan archivos distintos
        self.assertRegex(obs[0]["start"][11:19], r"^\d\d:\d\d:\d\d$")
        csv = self.C.get("/api/logs/csv").text.splitlines()
        self.assertTrue(csv[0].startswith("fecha,hora_inicio,hora_fin,fuente,tipo_fuente"))
        self.assertIn("b1/incoming/a.mp4", "\n".join(csv))
        fire_ev = [i for i in obs if i["key"] == "FIRE_OR_SMOKE"]
        self.assertTrue(fire_ev)

    def test_4_missing_file_errors_and_retry(self):
        r = self.C.post("/api/ingest", json={"bucket": "b1", "key": "incoming/no_existe.mp4"}, headers=H).json()
        it = self.wait([r["id"]])[r["id"]]
        self.assertEqual(it["status"], "error")
        self.assertIn("descargar", it["error"])
        make_video(_obs / "b1" / "incoming" / "no_existe.mp4")       # ya aparece: Reintentar
        self.assertEqual(self.C.post(f"/api/ingest/{r['id']}/retry").status_code, 200)
        self.assertEqual(self.wait([r["id"]])[r["id"]]["status"], "done")

    def test_5_resume_after_restart(self):
        iid = "deadbeef"
        vialert.INGESTS[iid] = {"id": iid, "bucket": "b1", "key": "incoming/a.mp4", "status": "analyzing", "received": time.time(),
                                "attempts": 0}
        vialert.ingest_save()
        vialert.INGESTS.clear()
        vialert.INGEST_KEYS.clear()
        redo = vialert.ingest_load()
        self.assertIn(iid, redo)
        self.assertEqual(vialert.INGESTS[iid]["status"], "queued")
        self.assertEqual(vialert.INGESTS[iid]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
