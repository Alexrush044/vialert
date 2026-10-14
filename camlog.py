"""Bitacora persistente por camara y por dia (no depende de app.py).

events/<camara>/<AAAA-MM-DD>/
    events.jsonl    cada deteccion de incidente (resultado del modelo distinto de trafico normal, persona caida o banderas)
    frames.jsonl    cada frame analizado (evento, confianza, latencia, tokens): sirve para las estadisticas del dia
    analytics.jsonl alertas de YOLO / calidad de camara (merodeo, objeto abandonado, camara tapada...)
    img/            frame de evidencia de cada deteccion
El dia se calcula en la zona horaria LOG_TZ (por defecto America/Mexico_City), no la del servidor.

Cada registro lleva su FUENTE: `source` (nombre de la camara, o bucket/archivo si vino de OBS) y `kind`
('camara' | 'obs'). `ts` es la hora de pared (zona LOG_TZ) en que se detecto; `t` es el segundo dentro del video.
"""
import csv
import io
import json
import os
import re
import shutil
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

EVENTS_DIR = Path(os.getenv("EVENTS_DIR", str(Path(__file__).parent / "events")))
KEEP_DAYS = int(os.getenv("EVENTS_KEEP_DAYS", "30"))
MERGE_S = float(os.getenv("EVENT_MERGE_S", "45"))      # detecciones iguales con menos de N s entre si = un solo incidente
EVENTS_DIR.mkdir(exist_ok=True)
_lock = threading.Lock()
_counts: dict[tuple[str, str], dict] = {}
SEV = {"low": 0, "medium": 1, "high": 2}
_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("LOG_TZ", "America/Mexico_City"))
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=-6))


TZ = _tz()


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def safe(s: str) -> str:
    if not s or not _SAFE.match(s) or s.startswith("."):
        raise ValueError("nombre invalido")
    return s


def day_dir(cam: str, day: str | None = None, create: bool = False) -> Path:
    d = EVENTS_DIR / safe(cam) / safe(day or today())
    if create:
        (d / "img").mkdir(parents=True, exist_ok=True)
    return d


def _append(p: Path, obj: dict):
    with _lock, open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def event_key(r: dict) -> str | None:
    """Clasifica un resultado como incidente: el evento principal, o PERSON_DOWN / la bandera si el evento es normal."""
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


def _counts_for(cam: str, day: str) -> dict:
    k = (cam, day)
    if k not in _counts:
        d = day_dir(cam, day)
        _counts[k] = {"frames": len(_read(d / "frames.jsonl")), "det": len(_read(d / "events.jsonl")),
                      "alerts": len(_read(d / "analytics.jsonl"))}
    return _counts[k]


def forget(cam: str) -> None:
    """Olvida los contadores en memoria de una camara eliminada."""
    for k in [k for k in _counts if k[0] == cam]:
        _counts.pop(k, None)


def counts(cam: str, day: str | None = None) -> dict:
    return dict(_counts_for(cam, day or today()))


def persist_result(cam: str, cam_name: str, job_id: str, ev: dict, log_dir: Path | None, source: str | None = None,
                   kind: str = "camara"):
    now = datetime.now(TZ)
    day = now.strftime("%Y-%m-%d")
    d = day_dir(cam, day, create=True)
    c = _counts_for(cam, day)
    base = {"ts": now.isoformat(timespec="seconds"), "epoch": round(now.timestamp(), 1), "cam": cam, "cam_name": cam_name,
            "job": job_id, "t": ev.get("t"), "idx": ev.get("idx"), "source": source or cam_name, "kind": kind}
    if ev.get("error"):
        _append(d / "frames.jsonl", {**base, "error": ev["error"][:200], "total_ms": ev.get("total_ms")})
        c["frames"] += 1
        return
    _append(d / "frames.jsonl", {**base, "event": ev.get("event"), "confidence": ev.get("confidence"),
                                 "severity": ev.get("severity"), "person_down": ev.get("person_down"),
                                 "flags": ev.get("flags"), "total_ms": ev.get("total_ms"), "why": ev.get("why"),
                                 "tokens_in": ev.get("tokens_in"), "tokens_out": ev.get("tokens_out")})
    c["frames"] += 1
    key = event_key(ev)
    if not key:
        return
    img = None
    files = ev.get("files") or []
    if log_dir and files:
        src = log_dir / files[-1]                      # el ultimo frame enviado es el actual
        if src.exists():
            img = f"{now.strftime('%H%M%S')}_{job_id[:4]}{ev.get('idx', 0)}_{key}.jpg"
            try:
                shutil.copyfile(src, d / "img" / img)
            except OSError:
                img = None
    _append(d / "events.jsonl", {**base, "key": key, "event": ev.get("event"), "confidence": ev.get("confidence"),
                                 "severity": ev.get("severity"), "person_down": ev.get("person_down"),
                                 "flags": ev.get("flags") or [], "reason": ev.get("reason"), "scene": ev.get("scene"),
                                 "why": ev.get("why"), "img": img})
    c["det"] += 1


def persist_alerts(cam: str, cam_name: str, job_id: str, events: list[dict], seen: set, source: str | None = None,
                   kind: str = "camara"):
    now = datetime.now(TZ)
    day = now.strftime("%Y-%m-%d")
    d = day_dir(cam, day, create=True)
    c = _counts_for(cam, day)
    for e in events:
        k = (e.get("why"), e.get("t"))
        if k in seen:
            continue
        seen.add(k)
        _append(d / "analytics.jsonl", {"ts": now.isoformat(timespec="seconds"), "epoch": round(now.timestamp(), 1), "cam": cam,
                                        "cam_name": cam_name, "job": job_id, "t": e.get("t"), "why": e.get("why"),
                                        "label": e.get("label"), "detail": e.get("detail"), "src": e.get("src"),
                                        "source": source or cam_name, "kind": kind})
        c["alerts"] += 1


def days(cam: str) -> list[str]:
    p = EVENTS_DIR / safe(cam)
    return sorted((x.name for x in p.iterdir() if x.is_dir()), reverse=True) if p.exists() else []


def incidents(cam: str, day: str) -> list[dict]:
    """Une detecciones consecutivas del mismo evento (hueco < MERGE_S) en un incidente."""
    rows = sorted(_read(day_dir(cam, day) / "events.jsonl"), key=lambda r: r.get("epoch", 0))
    by_key: dict[tuple, list[dict]] = {}
    for r in rows:                     # un incidente nunca mezcla fuentes distintas (p. ej. dos archivos de OBS seguidos)
        by_key.setdefault((r["key"], r.get("source") or r.get("cam_name") or cam, r.get("job") if r.get("kind") == "obs" else None), []).append(r)
    out = []
    for (key, _src, _job), rs in by_key.items():
        cur = None
        for r in rs:
            if cur and r["epoch"] - cur["end_epoch"] <= MERGE_S:
                cur["n"] += 1
                cur["end"], cur["end_epoch"] = r["ts"], r["epoch"]
                if (r.get("confidence") or 0) >= cur["confidence"]:
                    cur.update(confidence=r.get("confidence") or 0, reason=r.get("reason"), img=r.get("img") or cur["img"],
                               scene=r.get("scene") or cur.get("scene"))
                if SEV.get(r.get("severity"), 0) > SEV.get(cur["severity"], 0):
                    cur["severity"] = r.get("severity")
                cur["t_end"] = r.get("t")
                cur["person_down"] = cur["person_down"] or bool(r.get("person_down"))
                cur["flags"] = sorted(set(cur["flags"]) | set(r.get("flags") or []))
            else:
                if cur:
                    out.append(cur)
                cur = {"key": key, "event": r.get("event"), "start": r["ts"], "start_epoch": r["epoch"], "end": r["ts"],
                       "end_epoch": r["epoch"], "n": 1, "confidence": r.get("confidence") or 0, "severity": r.get("severity") or "low",
                       "person_down": bool(r.get("person_down")), "flags": list(r.get("flags") or []), "reason": r.get("reason"),
                       "img": r.get("img"), "cam": cam, "day": day, "source": r.get("source") or r.get("cam_name") or cam,
                       "kind": r.get("kind") or "camara", "job": r.get("job"), "t_start": r.get("t"), "t_end": r.get("t"),
                       "scene": r.get("scene")}
        if cur:
            out.append(cur)
    out.sort(key=lambda i: -i["start_epoch"])
    for n, i in enumerate(reversed(out), 1):
        i["id"] = n
    return out


def stats(cam: str, day: str) -> dict:
    d = day_dir(cam, day)
    fr = _read(d / "frames.jsonl")
    ok = [r for r in fr if not r.get("error")]
    lat = sorted(r["total_ms"] for r in ok if r.get("total_ms") is not None)
    tin = sum(r.get("tokens_in") or 0 for r in ok)
    tout = sum(r.get("tokens_out") or 0 for r in ok)
    ev_counts: dict[str, int] = {}
    for r in ok:
        k = event_key(r) or "NORMAL_TRAFFIC"
        ev_counts[k] = ev_counts.get(k, 0) + 1
    inc = incidents(cam, day)
    an = _read(d / "analytics.jsonl")
    an_counts: dict[str, int] = {}
    for a in an:
        an_counts[a["why"]] = an_counts.get(a["why"], 0) + 1
    p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else None  # noqa: E731
    return {"cam": cam, "day": day, "frames": len(fr), "errors": len(fr) - len(ok), "detections": len(_read(d / "events.jsonl")),
            "incidents": len(inc), "frame_events": ev_counts, "alerts": len(an), "alert_counts": an_counts,
            "p50_ms": p(0.5), "p95_ms": p(0.95), "tokens_in": tin, "tokens_out": tout,
            "first": fr[0]["ts"] if fr else None, "last": fr[-1]["ts"] if fr else None,
            "by_severity": {s: sum(1 for i in inc if i["severity"] == s) for s in ("high", "medium", "low")}}


def export_csv(cam: str, day: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEAD)
    for i in reversed(incidents(cam, day)):
        w.writerow(_csv_row(i))
    return buf.getvalue()


def prune():
    """Borra dias mas viejos que EVENTS_KEEP_DAYS."""
    cutoff = (datetime.now(TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for cam in EVENTS_DIR.iterdir():
        if not cam.is_dir():
            continue
        for d in cam.iterdir():
            if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name) and d.name < cutoff:
                shutil.rmtree(d, ignore_errors=True)


# ---- Consulta global de registros (varias camaras, rango de fechas, filtros) ------------------------
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def all_cams() -> list[str]:
    return sorted(p.name for p in EVENTS_DIR.iterdir() if p.is_dir() and _SAFE.match(p.name)) if EVENTS_DIR.exists() else []


def _in_range(day: str, d1: str | None, d2: str | None) -> bool:
    return bool(_DAY.match(day)) and (not d1 or day >= d1) and (not d2 or day <= d2)


def query(cams: list[str] | None = None, d1: str | None = None, d2: str | None = None, keys: list[str] | None = None,
          sev: list[str] | None = None, down: bool = False, q: str = "", limit: int = 500, names: dict | None = None) -> dict:
    """Incidentes de varias camaras/dias con filtros. Devuelve lista (mas recientes primero) y resumen."""
    for d in (d1, d2):
        if d and not _DAY.match(d):
            raise ValueError("fecha invalida")
    names = names or {}
    cams = [safe(c) for c in cams] if cams else all_cams()
    q = (q or "").strip().lower()
    base, out_days = [], set()
    for cam in cams:
        for day in days(cam):
            if not _in_range(day, d1, d2):
                continue
            out_days.add(day)
            for i in incidents(cam, day):
                i["cam_name"] = names.get(cam, cam)
                base.append(i)
    present = sorted({i["key"] for i in base})
    rows = []
    for i in base:
        if keys and i["key"] not in keys:
            continue
        if sev and i["severity"] not in sev:
            continue
        if down and not i["person_down"]:
            continue
        if q and q not in " ".join([i["key"], i.get("event") or "", i.get("reason") or "", i["cam_name"], i.get("source") or "", i.get("scene") or "", " ".join(i["flags"])]).lower():
            continue
        rows.append(i)
    rows.sort(key=lambda i: -i["start_epoch"])
    by_cam: dict[str, int] = {}
    by_day: dict[str, dict[str, int]] = {}
    by_key: dict[str, int] = {}
    for i in rows:
        by_cam[i["cam"]] = by_cam.get(i["cam"], 0) + 1
        by_day.setdefault(i["day"], {})
        by_day[i["day"]][i["cam"]] = by_day[i["day"]].get(i["cam"], 0) + 1
        by_key[i["key"]] = by_key.get(i["key"], 0) + 1
    return {"total": len(rows), "shown": min(len(rows), limit), "incidents": rows[:limit], "keys": present, "cams": cams,
            "by_cam": by_cam, "by_day": dict(sorted(by_day.items(), reverse=True)), "by_key": by_key,
            "by_severity": {s: sum(1 for i in rows if i["severity"] == s) for s in ("high", "medium", "low")},
            "person_down": sum(1 for i in rows if i["person_down"]), "days_with_data": sorted(out_days, reverse=True),
            "names": {c: names.get(c, c) for c in cams}}


CSV_HEAD = ["fecha", "hora_inicio", "hora_fin", "fuente", "tipo_fuente", "id_fuente", "evento", "severidad", "confianza",
            "detecciones", "persona_caida", "banderas", "segundo_en_video", "motivo", "escena", "evidencia"]


def _csv_row(i: dict) -> list:
    ts = lambda v: (v or "")[11:19]  # noqa: E731
    return [i["day"], ts(i["start"]), ts(i["end"]), i.get("source") or i.get("cam_name") or i["cam"], i.get("kind") or "camara",
            i["cam"], i["key"], i["severity"], round(i["confidence"], 2), i["n"], int(i["person_down"]), "|".join(i["flags"]),
            "" if i.get("t_start") is None else i["t_start"], (i["reason"] or "").replace("\n", " "),
            (i.get("scene") or "").replace("\n", " "), i["img"] or ""]


def csv_rows(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEAD)
    for i in rows:
        w.writerow(_csv_row(i))
    return buf.getvalue()
