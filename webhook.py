"""Envia cada incidente detectado a un webhook (HTTP POST con JSON). Solo libreria estandar.
.env:  WEBHOOK_URL=https://...          (vacio = desactivado)
       WEBHOOK_TOKEN=secreto            (opcional; se manda como  Authorization: Bearer <token>  y  X-Vialert-Token)
       WEBHOOK_MIN_SEV=low|medium|high  (por defecto low = todos los incidentes)
       WEBHOOK_IMAGE=0|1                (1 = incluye el frame de evidencia en base64 dentro del JSON)
       WEBHOOK_COOLDOWN_S=30            (no repite el mismo evento de la misma fuente antes de N s)
       WEBHOOK_ALERTS=0|1               (1 = tambien manda alertas de YOLO/camara: merodeo, objeto abandonado...)
"""
import base64
import json
import logging
import os
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("vialert")
URL = os.getenv("WEBHOOK_URL", "").strip()
TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
MIN_SEV = {"low": 0, "medium": 1, "high": 2}.get(os.getenv("WEBHOOK_MIN_SEV", "low").lower(), 0)
WITH_IMG = os.getenv("WEBHOOK_IMAGE", "0") == "1"
COOLDOWN = float(os.getenv("WEBHOOK_COOLDOWN_S", "30"))
ALERTS = os.getenv("WEBHOOK_ALERTS", "0") == "1"
SEV = {"low": 0, "medium": 1, "high": 2}
_last: dict[tuple, float] = {}


def enabled() -> bool:
    return bool(URL)


def event_key(r: dict) -> str | None:
    if r.get("error"):
        return None
    ev = r.get("event")
    if ev and ev != "NORMAL_TRAFFIC":
        return ev
    if r.get("person_down"):
        return "PERSON_DOWN"
    if r.get("flags"):
        return r["flags"][0]
    return None


def post(payload: dict) -> tuple[bool, str]:
    """POST sincrono con 2 reintentos. Devuelve (ok, detalle)."""
    if not URL:
        return False, "WEBHOOK_URL vacio"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdr = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "vialert/1"}
    if TOKEN:
        hdr["Authorization"] = f"Bearer {TOKEN}"
        hdr["X-Vialert-Token"] = TOKEN
    err = ""
    for n in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(URL, data=body, headers=hdr, method="POST"), timeout=8) as r:
                return True, f"HTTP {r.status}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:200]
            time.sleep(1 + n)
    return False, err


def send_result(source: str, job_id: str, ev: dict, log_dir: Path | None, kind: str = "camara") -> None:
    """Llamar desde un hilo. Filtra por severidad y enfriamiento y envia el incidente."""
    if not URL:
        return
    key = event_key(ev)
    if not key or SEV.get(ev.get("severity"), 0) < MIN_SEV:
        return
    now = time.time()
    k = (source, key)
    if now - _last.get(k, 0) < COOLDOWN:
        return
    _last[k] = now
    payload = {"type": "incident", "source": source, "source_kind": kind, "job_id": job_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "video_time_s": ev.get("t"), "event": key, "model_event": ev.get("event"), "severity": ev.get("severity"),
               "confidence": ev.get("confidence"), "person_down": bool(ev.get("person_down")), "flags": ev.get("flags") or [],
               "reason": ev.get("reason"), "scene": ev.get("scene")}
    if WITH_IMG and log_dir and ev.get("files"):
        try:
            payload["image_jpeg_base64"] = base64.b64encode((log_dir / ev["files"][-1]).read_bytes()).decode()
        except OSError:
            pass
    ok, d = post(payload)
    log.info("job=%s WEBHOOK %s %s -> %s", job_id, source, key, d if ok else "ERROR " + d)


def send_alerts(source: str, job_id: str, events: list[dict], seen: set, kind: str = "camara") -> None:
    if not URL or not ALERTS:
        return
    for e in events:
        k = (e.get("why"), e.get("t"))
        if k in seen:
            continue
        seen.add(k)
        ok, d = post({"type": "alert", "source": source, "source_kind": kind, "job_id": job_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      "alert": e.get("why"), "label": e.get("label"), "detail": e.get("detail"), "video_time_s": e.get("t")})
        log.info("job=%s WEBHOOK alerta %s -> %s", job_id, e.get("why"), d if ok else "ERROR " + d)


def test_payload() -> dict:
    return {"type": "test", "source": "vialert", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "COLLISION", "severity": "high",
            "confidence": 0.93, "person_down": False, "flags": [], "reason": "Mensaje de prueba de Vialert", "scene": "prueba"}
