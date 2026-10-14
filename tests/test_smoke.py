"""Pruebas de humo (sin red, sin MaaS, sin camaras reales):   python -m unittest discover -s tests -v"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402,F401  (fija el entorno antes de importar app)
_tmp = _env.TMP
from fastapi.testclient import TestClient  # noqa: E402

import app as vialert  # noqa: E402

C = TestClient(vialert.app)
H = {"Host": "demo.example.com:8000"}


class Smoke(unittest.TestCase):
    def test_01_health_config(self):
        d = C.get("/healthz").json()
        self.assertTrue(d["ok"])
        cfg = C.get("/api/config").json()
        self.assertTrue(cfg["mock"])
        self.assertIn("version", cfg)

    def test_02_phone_camera(self):
        r = C.post("/api/cams", json={"type": "publish", "name": "Celular"}, headers=H)
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["type"], "publish")
        k = d["connect"]
        self.assertEqual(k["rtsp"], "rtsp://demo.example.com:8554/" + d["id"])
        self.assertEqual((k["user"], k["password"]), ("cam", "Clave123"))
        self.assertIn("larix://", k["larix"])
        self.assertEqual(C.get(f"/api/cams/{d['id']}/connect", headers=H).json()["rtmp"], f"rtmp://demo.example.com:1935/{d['id']}")
        self.assertTrue(C.get("/api/qr", params={"data": k["rtsp"]}).text.startswith("<svg"))

    def test_03_ip_camera_keeps_password(self):
        r = C.post("/api/cams", json={"type": "ip", "name": "Entrada", "host": "192.168.1.64", "port": 554, "user": "admin",
                                       "pass": "p@ss/1", "path": "/Streaming/Channels/102"})
        self.assertEqual(r.status_code, 200)
        cam = r.json()
        self.assertNotIn("p@ss", json.dumps(cam))                      # la clave nunca sale al front
        self.assertTrue(cam["has_auth"])
        saved = json.loads((Path(_tmp) / "cameras.json").read_text())["cams"][cam["id"]]["url"]
        self.assertEqual(saved, "rtsp://admin:p%40ss%2F1@192.168.1.64:554/Streaming/Channels/102")
        r2 = C.put(f"/api/cams/{cam['id']}", json={"name": "Entrada 2", "host": "192.168.1.65", "user": "admin", "port": 554, "path": "/x"})
        self.assertEqual(r2.json()["ip"]["host"], "192.168.1.65")
        saved = json.loads((Path(_tmp) / "cameras.json").read_text())["cams"][cam["id"]]["url"]
        self.assertIn("p%40ss%2F1@192.168.1.65", saved)                 # clave vacia = conservar

    def test_03b_ip_camera_without_credentials(self):
        r = C.post("/api/cams", json={"type": "ip", "name": "Patio", "host": "10.0.0.7", "path": "/live"})
        self.assertEqual(r.status_code, 200)
        cam = r.json()
        self.assertFalse(cam["has_auth"])
        saved = json.loads((Path(_tmp) / "cameras.json").read_text())["cams"][cam["id"]]["url"]
        self.assertEqual(saved, "rtsp://10.0.0.7/live")
        C.delete(f"/api/cams/{cam['id']}?purge=1")

    def test_04_validation_and_limit(self):
        self.assertEqual(C.post("/api/cams", json={"type": "ip", "url": "http://x/y"}).status_code, 400)
        self.assertEqual(C.post("/api/cams", json={"type": "ip", "host": ""}).status_code, 400)
        self.assertEqual(C.post("/api/cams", json={"type": "nada"}).status_code, 400)
        while len(vialert.CAMS) < 3:
            C.post("/api/cams", json={"type": "publish"})
        self.assertEqual(C.post("/api/cams", json={"type": "publish"}).status_code, 409)

    def test_05_probe_diagnostics(self):
        d = C.post("/api/cams/probe", json={"host": "127.0.0.1", "port": 1, "path": "/x"}).json()
        self.assertFalse(d["ok"])
        self.assertIn(d["code"], ("refused", "unreachable", "error"))
        self.assertNotIn("secreto", json.dumps(C.post("/api/cams/probe", json={"url": "rtsp://u:secreto@127.0.0.1:1/x"}).json()))

    def test_06_delete_and_ids_not_reused(self):
        ids = list(vialert.CAMS)
        self.assertEqual(C.delete(f"/api/cams/{ids[0]}?purge=1").status_code, 200)
        new = C.post("/api/cams", json={"type": "publish"}).json()["id"]
        self.assertNotIn(new, ids)

    def test_07_logs_query_and_webhook(self):
        self.assertEqual(C.get("/api/logs").status_code, 200)
        self.assertEqual(C.get("/api/logs", params={"frm": "mal"}).status_code, 400)
        self.assertEqual(C.post("/api/webhook/test").status_code, 400)      # sin WEBHOOK_URL

    def test_08_static_ui(self):
        t = C.get("/").text
        for needle in ("camAdd", "cmodal", "logview"):
            self.assertIn(needle, t)


if __name__ == "__main__":
    unittest.main()
