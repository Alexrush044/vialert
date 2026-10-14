#!/usr/bin/env python3
"""Verifica una instalacion de Vialert y dice que falta.   python3 tools/selfcheck.py [URL] [usuario clave]
Ejemplo:  python3 tools/selfcheck.py http://localhost:8000 admin mi_clave"""
import base64
import json
import socket
import sys
import urllib.request
from urllib.parse import urlsplit

url = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
auth = {}
if len(sys.argv) > 3:
    auth = {"Authorization": "Basic " + base64.b64encode(f"{sys.argv[2]}:{sys.argv[3]}".encode()).decode()}
bad = 0


def line(ok, text, hint="", optional=False):
    """optional=True: solo aplica si usaras celulares/equipos que transmiten (no cuenta como error)."""
    global bad
    bad += (not ok and not optional)
    tag = "  OK   " if ok else ("  AVISO" if optional else "  FALTA")
    print(tag + " " + text + (("\n         -> " + hint) if (hint and not ok) else ""))


def get(path):
    req = urllib.request.Request(url + path, headers=auth)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


print(f"Vialert en {url}")
try:
    h = get("/healthz")
    line(True, f"servidor arriba (version {h.get('version')})")
except Exception as e:  # noqa: BLE001
    line(False, "el servidor no responde", f"{type(e).__name__}. ¿Esta corriendo? docker compose ps  /  ./run.sh")
    sys.exit(1)
try:
    c = get("/api/config")
except Exception as e:  # noqa: BLE001
    line(False, "no se pudo leer /api/config", "Si pusiste DEMO_USER/DEMO_PASS, pasalos: selfcheck.py URL usuario clave")
    sys.exit(1)
line(not c.get("mock"), "MAAS_API_KEY configurada (analisis real)", "Define MAAS_API_KEY en .env y reinicia (hoy corre SIMULADO)")
line(bool(c.get("auth")), "acceso protegido con usuario y clave", "Define DEMO_USER y DEMO_PASS en .env")
line(bool(c.get("yolo_available")), "YOLO disponible", "Revisa que la instalacion de PyTorch/Ultralytics termino (YOLO=0 lo desactiva a proposito)")
if c.get("yolo"):
    ok = c.get("yolo_ok")
    if ok is None:
        print("  INFO YOLO esta cargando (espera unos segundos y repite)")
    else:
        line(bool(ok), f"YOLO cargo el modelo ({c.get('yolo_model')})", f"{c.get('yolo_error')}. Sin YOLO se pierden detecciones. Revisa models/ y los logs")
line(bool(c.get("fire")), "detector local de fuego/humo activo", "FIRE=0 en .env lo apaga; quitalo para activarlo", optional=True)
if (c.get("obs") or {}).get("configured"):
    try:
        q = get("/api/ingest/list")
        n = q.get("counts", {})
        print(f"  INFO OBS: cola con {n.get('queued', 0)} en espera, {n.get('analyzing', 0) + n.get('downloading', 0)} en proceso, {n.get('done', 0)} completos, {n.get('error', 0)} con error ({q.get('workers')} trabajador(es))")
    except Exception as e:  # noqa: BLE001
        line(False, "no se pudo leer la cola de OBS", str(e))

try:
    cams = get("/api/cams")
    pub = cams.get("publish", {})
    line(bool(pub.get("has_password")), "clave de publicacion conocida (celulares)", "Solo si usaras celulares/equipos que transmiten: define MTX_PUBLISH_PASS (o instala MediaMTX). Las camaras IP no lo necesitan", optional=True)
    print(f"  INFO {len(cams['cams'])} camaras registradas, maximo {cams.get('max')}")
except Exception as e:  # noqa: BLE001
    line(False, "no se pudo leer /api/cams", str(e))
host = urlsplit(url).hostname or "localhost"
for name, port in (("RTSP", 8554), ("RTMP", 1935)):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        line(True, f"puerto {port} ({name}) abierto en {host}")
    except OSError:
        line(False, f"puerto {port} ({name}) cerrado en {host}", "Solo si usaras celulares/equipos que transmiten: levanta MediaMTX y abre el puerto. Las camaras IP no lo necesitan", optional=True)
    finally:
        s.close()
print("\nTodo en orden." if not bad else f"\n{bad} punto(s) por resolver.")
sys.exit(1 if bad else 0)
