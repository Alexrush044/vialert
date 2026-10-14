"""VIALERT - deteccion de incidentes viales con VLM (Huawei Cloud MaaS).

Pipeline: captura (1 frame cada FRAME_INTERVAL s de video) separada del analisis
(workers asincronos). Un frame lento nunca bloquea al siguiente.
Sin MAAS_API_KEY corre en modo MOCK (resultados simulados, solo para probar el front).
"""
import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import secrets
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import cv2
import httpx
import numpy as np

import detector
import i18n
import camlog
import live
import obsio
import webhook
import quality
import fire
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from hub import Hub
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
UPLOADS = BASE / "uploads"
CASES_DIR = BASE / "casos"
LOGS = BASE / "logs"
LOGS.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
CASES_DIR.mkdir(exist_ok=True)

# ---- Configuracion (variables de entorno) ---------------------------------
MAAS_API_KEY = os.getenv("MAAS_API_KEY", "").strip()
MAAS_URL = os.getenv("MAAS_URL", "https://api-ap-southeast-1.modelarts-maas.com/v2/chat/completions")
MAAS_MODEL = os.getenv("MAAS_MODEL", "deepseek-v4.1-flash")
FRAME_INTERVAL = float(os.getenv("FRAME_INTERVAL", "5"))    # s de video entre frames
CONTEXT_FRAMES = int(os.getenv("CONTEXT_FRAMES", "1"))      # frames previos como contexto (0 = solo actual)
FRAME_WIDTH = int(os.getenv("FRAME_WIDTH", "960"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "80"))
TIMEOUT_S = float(os.getenv("MAAS_TIMEOUT", "7"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "150"))
WORKERS = int(os.getenv("WORKERS", "3"))
LIVE_URL = os.getenv("LIVE_URL", "rtsp://127.0.0.1:8554/cam1")        # stream por defecto (MediaMTX en la propia ECS)
LIVE_MAX_MIN = float(os.getenv("LIVE_MAX_MIN", "30"))                  # corte automatico de una sesion en vivo (cuida tokens)
LIVE_WINDOW_S = float(os.getenv("LIVE_WINDOW_S", "60"))                # ventana de analisis YOLO en vivo
LIVE_ANALYZE_EVERY = float(os.getenv("LIVE_ANALYZE_EVERY", "2"))       # cada cuantos s se recalculan las reglas de YOLO
LIVE_CONNECT_S = float(os.getenv("LIVE_CONNECT_S", "20"))              # espera maxima al primer frame
LIVE_COOLDOWN = {"yolo:merodeo": 60, "yolo:objeto_abandonado": 60, "yolo:estacionado_prohibido": 60,
                 "yolo:intrusion": 20, "yolo:aglomeracion": 30, "fire:fuego": 20, "fire:humo": 45,
                 "yolo:animal": 45, "yolo:objeto_peligroso": 30}
LIVE_COOLDOWN_DEFAULT = 12
CAM_MAX = int(os.getenv("CAM_MAX", os.getenv("CAM_SLOTS", "8")))        # camaras registrables a la vez (cada activa consume CPU y tokens)
MTX_READ_URL = os.getenv("MTX_READ_URL", "rtsp://127.0.0.1:8554").rstrip("/")   # desde donde Vialert lee lo que publican los celulares
MTX_PUBLISH_USER = os.getenv("MTX_PUBLISH_USER", "cam")
MTX_PUBLISH_PASS = os.getenv("MTX_PUBLISH_PASS", "")
MTX_CONFIG = os.getenv("MTX_CONFIG", "/opt/mediamtx/mediamtx.yml")      # si Vialert y MediaMTX comparten maquina, de aqui se lee la clave
MTX_RTSP_PORT = int(os.getenv("MTX_RTSP_PORT", "8554"))
MTX_RTMP_PORT = int(os.getenv("MTX_RTMP_PORT", "1935"))
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")                              # IP/DNS que ven los celulares; vacio = el host con que abres esta pagina
CAM_MAX_MIN = float(os.getenv("CAM_MAX_MIN", "120"))                    # corte automatico por sesion de camara (cuida tokens)
CAM_LOG_KEEP_MIN = float(os.getenv("CAM_LOG_KEEP_MIN", "30"))           # frames temporales de cada sesion (la evidencia se guarda aparte)
CAMS: dict[str, dict] = {}
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")                         # token que debe mandar FunctionGraph a /api/ingest
INGESTS: dict[str, dict] = {}                                           # videos recibidos desde OBS (Escenario 2)
INGEST_KEYS: dict[str, str] = {}                                        # bucket/key -> id (evita duplicados)
RPM_LIMIT = int(os.getenv("RPM_LIMIT", "90"))  # MaaS permite 100 RPM; 0 = sin limite
_rpm_hits: list[float] = []
_rpm_lock = None


async def rpm_wait():
    """Ventana deslizante de 60 s compartida por todos los jobs: espera si se alcanzo RPM_LIMIT."""
    global _rpm_lock
    if RPM_LIMIT <= 0:
        return
    if _rpm_lock is None:
        _rpm_lock = asyncio.Lock()
    async with _rpm_lock:
        while True:
            now = time.monotonic()
            while _rpm_hits and now - _rpm_hits[0] >= 60:
                _rpm_hits.pop(0)
            if len(_rpm_hits) < RPM_LIMIT:
                _rpm_hits.append(now)
                return
            await asyncio.sleep(max(0.05, 60 - (now - _rpm_hits[0])))
BUDGET_MS = int(os.getenv("BUDGET_MS", "7000"))
MIN_CONF = float(os.getenv("MIN_CONF", "0.5"))              # confianza minima para contar una deteccion en el veredicto
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
DEMO_USER = os.getenv("DEMO_USER", "")
DEMO_PASS = os.getenv("DEMO_PASS", "")
AUTOCROP = os.getenv("AUTOCROP", "0") == "1"                # recorta bordes negros (grabaciones de pantalla)
CONTEXT_GAP_S = float(os.getenv("CONTEXT_GAP_S", "1.5"))    # el frame de contexto se toma N s antes del actual
ADAPTIVE = os.getenv("ADAPTIVE", "1") == "1"                # muestreo hibrido: base cada FRAME_INTERVAL + picos de movimiento
SCAN_FPS = float(os.getenv("SCAN_FPS", "4"))                # frames/s del pre-escaneo de movimiento (barato, local)
MOTION_SIGMA = float(os.getenv("MOTION_SIGMA", "2.2"))      # umbral robusto para considerar un pico de movimiento
MIN_GAP_S = float(os.getenv("MIN_GAP_S", "1.2"))            # separacion minima entre frames enviados
MAX_EXTRA_PER_MIN = int(os.getenv("MAX_EXTRA_PER_MIN", "14"))  # tope de frames extra por minuto de video
DESCRIBE = os.getenv("DESCRIBE", "1") == "1"                # el modelo describe la escena (algo mas de latencia)
YOLO_ON = os.getenv("YOLO", "1") == "1" and detector.available()   # YOLO por defecto si esta instalado
QUALITY_ON = os.getenv("QUALITY", "1") == "1"                        # analitica de calidad de camara (local)
YOLO_HINTS = os.getenv("YOLO_HINTS", "1") == "1"                    # enviar pistas de YOLO al VLM
MOCK = not MAAS_API_KEY
MODELS_URL = MAAS_URL.replace("/chat/completions", "/models")

LABELS = ["COLLISION", "VEHICLE_LOSS_OF_CONTROL", "STOPPED_VEHICLE", "LANE_OBSTRUCTION",
          "DEBRIS", "TRAFFIC_CONGESTION", "DANGEROUS_MANEUVER", "FIRE_OR_SMOKE",
          "PEDESTRIAN_ON_ROAD", "ASSAULT_OR_ROBBERY", "FIGHT_OR_VIOLENCE", "PROTEST_OR_BLOCKADE",
          "CROWD_GATHERING", "VANDALISM_OR_DAMAGE", "WRONG_WAY_VEHICLE", "PEDESTRIAN_STRUCK",
          "ILLEGAL_PARKING", "ABANDONED_OBJECT", "CAMERA_OBSTRUCTED", "LOW_VISIBILITY_OR_FLOOD",
          "RISK_BEHAVIOR", "NORMAL_TRAFFIC"]
FLAGS = ["armed_person", "no_helmet", "moto_overload"]
NORMAL = "NORMAL_TRAFFIC"
SEV = {"low": 0, "medium": 1, "high": 2}

_PROMPT_HEAD = (
    "Eres un sistema de deteccion de incidentes viales y de seguridad ciudadana que analiza imagenes de camaras de trafico o de seguridad "
    "(pueden ser oscuras, de baja resolucion o estar en escala de grises). "
    "Recibiras 1 o 2 imagenes: si son dos, la PRIMERA es de 1-2 segundos antes y la ULTIMA es el momento actual. "
    "Clasifica el evento principal de la ULTIMA imagen, usando la anterior para ver si algo cambio o se detuvo. "
    "Etiquetas permitidas: " + ", ".join(LABELS) + ". "
    "Definiciones: COLLISION=choque o impacto entre vehiculos, o vehiculo contra un objeto (busca vehiculos "
    "en contacto, danos, piezas caidas, vehiculos en posiciones anormales); "
    "VEHICLE_LOSS_OF_CONTROL=vehiculo derrapando, girando o fuera de trayectoria; "
    "STOPPED_VEHICLE=vehiculo detenido en carril o acotamiento; LANE_OBSTRUCTION=carril bloqueado; "
    "DEBRIS=objetos o escombros en la via; TRAFFIC_CONGESTION=trafico denso y lento; "
    "DANGEROUS_MANEUVER=maniobra brusca o reversa; FIRE_OR_SMOKE=fuego o humo; "
    "PEDESTRIAN_ON_ROAD=persona sobre el arroyo vehicular. "
    "Eventos entre personas (describe solo conductas visibles; no identifiques a nadie ni describas rasgos fisicos): "
    "ASSAULT_OR_ROBBERY=asalto o robo en curso: una persona somete, amenaza (arma visible, ademan de amenaza) o "
    "arrebata pertenencias a otra, o forcejeo por un objeto; FIGHT_OR_VIOLENCE=pelea o golpes: personas que se "
    "golpean, patean, empujan con fuerza o una persona agredida en el suelo mientras otros la rodean; "
    "PROTEST_OR_BLOCKADE=manifestacion, paro o bloqueo: grupo con pancartas, banderas o consignas, o personas "
    "bloqueando deliberadamente una via o acceso (llantas, vallas, cadenas humanas); "
    "CROWD_GATHERING=aglomeracion o evento social: muchas personas reunidas sin violencia ni bloqueo (concierto, "
    "mitin, fila, festejo). Para estos eventos exige evidencia visible (contacto fisico, arma, forcejeo, pancartas, "
    "bloqueo); una simple reunion o personas caminando cerca NO es pelea ni asalto. Si la imagen es poco clara, "
    "baja la confidence en lugar de afirmar. "
    "Otros eventos: VANDALISM_OR_DAMAGE=vandalismo, grafiti o dano intencional a propiedad (romper vidrios, golpear "
    "o rayar vehiculos o mobiliario); WRONG_WAY_VEHICLE=vehiculo que circula en sentido contrario al flujo de los "
    "demas; PEDESTRIAN_STRUCK=persona atropellada (vehiculo en contacto con una persona, o persona en el suelo junto a "
    "un vehiculo que la acaba de golpear; usalo en lugar de COLLISION); ILLEGAL_PARKING=vehiculo en doble fila, sobre "
    "banqueta, paso peatonal o zona claramente prohibida; ABANDONED_OBJECT=mochila, bolsa o bulto sin ninguna persona "
    "cerca; CAMERA_OBSTRUCTED=camara tapada, desenfocada, movida o con lente sucio que impide ver la escena; "
    "LOW_VISIBILITY_OR_FLOOD=niebla, lluvia intensa o inundacion/encharcamiento que dificulta la circulacion; "
    "RISK_BEHAVIOR=conducta de riesgo cuando NO aplica ningun otro evento pero si alguna bandera de flags. "
    "flags (lista, casi siempre vacia): armed_person=persona con arma visible (pistola, cuchillo, machete, garrote), "
    "solo si la ves con claridad y nunca por objetos cotidianos; no_helmet=motociclista o ciclista en marcha sin "
    "casco; moto_overload=motocicleta con 3 o mas ocupantes. "
    "Pistas de un incidente reciente: personas reunidas alrededor de vehiculos detenidos o danados, personas en el "
    "suelo, vehiculos atravesados o con las luces de emergencia. "
    "Si no hay evidencia visible responde NORMAL_TRAFFIC; no inventes incidentes. Si dudas entre dos etiquetas, "
    "incluye ambas en events y usa confidence baja. "
    "person_down=true SOLO si ves una persona tendida o caida. "
)


_PROMPT_HEAD_EN = (
    "You are a road-incident and public-safety detection system that analyzes images from traffic or security cameras "
    "(they may be dark, low-resolution or grayscale). "
    "You will receive 1 or 2 images: if two, the FIRST is from 1-2 seconds earlier and the LAST is the current moment. "
    "Classify the main event in the LAST image, using the earlier one to see whether something changed or stopped. "
    "Allowed labels: " + ", ".join(LABELS) + ". "
    "Definitions: COLLISION=crash or impact between vehicles, or a vehicle against an object (look for vehicles "
    "in contact, damage, fallen parts, vehicles in abnormal positions); "
    "VEHICLE_LOSS_OF_CONTROL=vehicle skidding, spinning or off its path; "
    "STOPPED_VEHICLE=vehicle stopped in a lane or on the shoulder; LANE_OBSTRUCTION=blocked lane; "
    "DEBRIS=objects or rubble on the road; TRAFFIC_CONGESTION=dense, slow traffic; "
    "DANGEROUS_MANEUVER=abrupt maneuver or reversing; FIRE_OR_SMOKE=fire or smoke; "
    "PEDESTRIAN_ON_ROAD=person on the roadway. "
    "Events between people (describe only visible behavior; do not identify anyone or describe physical traits): "
    "ASSAULT_OR_ROBBERY=assault or robbery in progress: one person overpowers, threatens (visible weapon, threatening gesture) or "
    "snatches belongings from another, or a struggle over an object; FIGHT_OR_VIOLENCE=fight or violence: people "
    "hitting, kicking, shoving hard, or a person attacked on the ground while others surround them; "
    "PROTEST_OR_BLOCKADE=protest, strike or blockade: group with banners, flags or chants, or people "
    "deliberately blocking a road or access (tires, barriers, human chains); "
    "CROWD_GATHERING=crowd or social event: many people gathered without violence or blockade (concert, "
    "rally, queue, celebration). For these events require visible evidence (physical contact, weapon, struggle, banners, "
    "blockade); a mere gathering or people walking nearby is NOT a fight or assault. If the image is unclear, "
    "lower the confidence instead of asserting. "
    "Other events: VANDALISM_OR_DAMAGE=vandalism, graffiti or intentional damage to property (breaking glass, hitting "
    "or scratching vehicles or furniture); WRONG_WAY_VEHICLE=vehicle driving against the flow of the "
    "others; PEDESTRIAN_STRUCK=person hit by a vehicle (vehicle in contact with a person, or a person on the ground next to "
    "a vehicle that just hit them; use it instead of COLLISION); ILLEGAL_PARKING=vehicle double-parked, on the "
    "sidewalk, crosswalk or clearly prohibited zone; ABANDONED_OBJECT=backpack, bag or package with no person "
    "nearby; CAMERA_OBSTRUCTED=camera covered, out of focus, moved or with a dirty lens that prevents seeing the scene; "
    "LOW_VISIBILITY_OR_FLOOD=fog, heavy rain or flooding/standing water hindering traffic; "
    "RISK_BEHAVIOR=risk behavior when NO other event applies but some flag in flags does. "
    "flags (list, almost always empty): armed_person=person with a visible weapon (gun, knife, machete, club), "
    "only if clearly seen and never for everyday objects; no_helmet=motorcyclist or cyclist riding without a "
    "helmet; moto_overload=motorcycle with 3 or more riders. "
    "Clues of a recent incident: people gathered around stopped or damaged vehicles, people on the "
    "ground, vehicles sitting across lanes or with hazard lights on. "
    "If there is no visible evidence answer NORMAL_TRAFFIC; do not invent incidents. If you doubt between two labels, "
    "include both in events and use low confidence. "
    "person_down=true ONLY if you see a person lying or fallen. "
)


def system_prompt(describe: bool) -> str:
    if i18n.en():
        if describe:
            return (_PROMPT_HEAD_EN + "First describe in scene what you see (max 25 words: vehicles, people, road condition, damage) "
                    "and then classify. Reply ONLY with JSON, no extra text: "
                    '{"scene":"<objective description in English>","event":"<LABEL>","events":["<LABEL>"],"person_down":false,"flags":[],'
                    '"confidence":0.0,"severity":"low|medium|high","reason":"<max 14 words in English, with the visible evidence>"}')
        return (_PROMPT_HEAD_EN + "Reply ONLY with JSON, no extra text: "
                '{"event":"<LABEL>","events":["<LABEL>"],"person_down":false,"flags":[],"confidence":0.0,'
                '"severity":"low|medium|high","reason":"<max 14 words in English, with the visible evidence>"}')
    return _system_prompt_es(describe)


def _system_prompt_es(describe: bool) -> str:
    if describe:
        return (_PROMPT_HEAD + "Primero describe en scene lo que ves (maximo 25 palabras: vehiculos, personas, estado de "
                "la via, danos) y despues clasifica. Responde UNICAMENTE con JSON, sin texto adicional: "
                '{"scene":"<descripcion objetiva>","event":"<ETIQUETA>","events":["<ETIQUETA>"],"person_down":false,"flags":[],'
                '"confidence":0.0,"severity":"low|medium|high","reason":"<maximo 14 palabras en español, con la evidencia visible>"}')
    return (_PROMPT_HEAD + "Responde UNICAMENTE con JSON, sin texto adicional: "
            '{"event":"<ETIQUETA>","events":["<ETIQUETA>"],"person_down":false,"flags":[],"confidence":0.0,'
            '"severity":"low|medium|high","reason":"<maximo 14 palabras en español, con la evidencia visible>"}')


SYSTEM_PROMPT = _system_prompt_es(False)

VERSION = "1.0.0"
app = FastAPI(title="VIALERT", version=VERSION)
JOBS: dict[str, dict] = {}

log = logging.getLogger("vialert")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    for _h in (logging.StreamHandler(), RotatingFileHandler(LOGS / "vialert.log", maxBytes=1_000_000, backupCount=3,
                                                            encoding="utf-8")):
        _h.setFormatter(_fmt)
        log.addHandler(_h)
    log.propagate = False


# ---- Autenticacion basica opcional (DEMO_USER / DEMO_PASS) ---------------------
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if DEMO_USER and DEMO_PASS and request.url.path != "/healthz" and not (request.url.path == "/api/ingest" and request.method == "POST"):
        ok = False
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, DEMO_USER) and secrets.compare_digest(p, DEMO_PASS)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="VIALERT"'})
    return await call_next(request)


# ---- Video ------------------------------------------------------------------
PLAYABLE = {"h264", "vp8", "vp9", "av1"}


def prepare_video(path: Path) -> Path:
    """Devuelve una copia reproducible en navegador (H.264/mp4) si hace falta."""
    if not (shutil.which("ffprobe") and shutil.which("ffmpeg")):
        return path
    try:
        codec = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return path
    if codec in PLAYABLE and path.suffix.lower() in {".mp4", ".webm"}:
        return path
    out = UPLOADS / f"{path.stem}.play.mp4"
    if out.exists():
        return out
    vcodec = ["-c:v", "copy"] if codec == "h264" else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"]
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-an", *vcodec,
                        "-movflags", "+faststart", str(out)], capture_output=True, timeout=900)
    return out if r.returncode == 0 and out.exists() else path


def detect_crop(cap, total_frames: float):
    """Caja (x0,y0,x1,y1) del contenido util, ignorando bordes negros. None si no hace falta recortar."""
    samples = []
    for frac in (0.1, 0.5, 0.9):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total_frames * frac))
        ok, img = cap.read()
        if ok:
            samples.append(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype("float32"))
    if not samples:
        return None
    g = np.mean(samples, axis=0)
    h, w = g.shape
    mask = g > 20
    rows = np.where(mask.mean(axis=1) > 0.15)[0]
    if not len(rows):
        return None
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    cols = np.where(mask[y0:y1].mean(axis=0) > 0.15)[0]
    if not len(cols):
        return None
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    m = int(0.01 * max(w, h))
    x0, y0, x1, y1 = max(0, x0 - m), max(0, y0 - m), min(w, x1 + m), min(h, y1 + m)
    area = (x1 - x0) * (y1 - y0) / (w * h)
    outside = np.ones_like(mask, dtype=bool)
    outside[y0:y1, x0:x1] = False
    if area > 0.85 or area < 0.05 or (outside.any() and g[outside].mean() > 25):
        return None
    return (x0, y0, x1, y1)


def grab_at(cap, t: float, crop):
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
    ok, img = cap.read()
    if not ok:
        return None
    return img[crop[1]:crop[3], crop[0]:crop[2]] if crop else img


def scan_video(cap, fps: float, crop, use_yolo: bool, progress: dict):
    """Pre-escaneo local: movimiento y calidad de imagen a SCAN_FPS y, si se pide, YOLO a YOLO_FPS.
    -> (movimiento, muestras YOLO, seg YOLO, muestras de calidad, muestras de fuego/humo)."""
    step = max(1, round(fps / SCAN_FPS))
    ystep = max(1, round(SCAN_FPS / detector.YOLO_FPS))
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    prev, motion, i, n = None, [], 0, 0
    qsamples: list = []
    fsamples: list = []
    fprev = None
    ybuf: list = []
    ysamples: list = []
    ysec = 0.0

    def flush():
        nonlocal ysec
        if not ybuf:
            return
        t0 = time.perf_counter()
        res = detector.detect([im for _, im in ybuf])
        ysec += time.perf_counter() - t0
        for (tt, _), dets in zip(ybuf, res):
            ysamples.append((tt, dets))
        ybuf.clear()

    while True:
        if i % step == 0:
            ok, img = cap.read()
        else:
            ok, img = cap.grab(), None
        if not ok:
            break
        if img is not None:
            t = i / fps
            view = img[crop[1]:crop[3], crop[0]:crop[2]] if crop else img
            g, qm = quality.metrics(view, prev)
            if prev is not None:
                motion.append((t, qm["diff"]))
            if QUALITY_ON:
                qsamples.append((t, qm))
            if fire.ON:
                fprev, fm = fire.metrics(view, fprev)
                fsamples.append((t, fm))
            prev = g
            if use_yolo and n % ystep == 0:
                ybuf.append((t, img))
                if len(ybuf) >= detector.YOLO_BATCH:
                    flush()
            n += 1
        progress["pct"] = min(1.0, i / total)
        i += 1
    flush()
    return motion, ysamples, ysec, qsamples, fsamples


def plan_times(duration: float, motion: list[tuple[float, float]], triggers: list[dict] | None = None) -> list[tuple[float, str]]:
    """Frames a analizar: base cada FRAME_INTERVAL + extras por disparadores de YOLO y picos de movimiento."""
    limit = max(duration - 0.2, 0.01)
    base = [(i * FRAME_INTERVAL, "base") for i in range(int(duration // FRAME_INTERVAL) + 1)]
    base = [(t, w) for t, w in base if t < limit] or [(0.0, "base")]
    extras: list[tuple[float, str]] = []
    cap_n = max(2, int(np.ceil(MAX_EXTRA_PER_MIN * duration / 60)))

    def free(t: float, gap: float) -> bool:
        return all(abs(t - u) >= MIN_GAP_S / 2 for u, _ in base) and all(abs(t - u) >= gap for u, _ in extras)

    # fuego/humo primero (son lo mas urgente), luego los demas disparadores por tiempo
    for tr in sorted(triggers or [], key=lambda x: (x.get("src") != "fire", x["t"])):
        t = min(tr["t"] + 0.3, limit)
        if len(extras) < cap_n and free(t, MIN_GAP_S / 2):
            extras.append((round(t, 2), tr["why"]))
    if ADAPTIVE and len(motion) >= 8:
        sc = np.array([m for _, m in motion])
        med = float(np.median(sc))
        mad = float(np.median(np.abs(sc - med))) * 1.4826 or 0.5
        thr = max(med + MOTION_SIGMA * mad, med * 1.8, 1.5)
        peaks = [i for i in range(1, len(sc) - 1) if sc[i] >= thr and sc[i] >= sc[i - 1] and sc[i] >= sc[i + 1]]
        peaks.sort(key=lambda i: -sc[i])
        for i in peaks:
            if len(extras) >= cap_n:
                break
            t = min(motion[i][0] + 0.5, limit)       # justo despues del pico: se ve el resultado del evento
            if free(t, MIN_GAP_S):
                extras.append((round(t, 2), "movimiento"))
    return sorted(base + extras)


def encode_frame(img, width: int, quality: int) -> str:
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode()


# ---- Cliente MaaS ---------------------------------------------------------------
class MaasError(Exception):
    FRIENDLY = {
        "81011": "Bloqueado por ModelArts Guard (contenido sensible)",
        "81003": "API key invalida o restringida (IP/modelo/endpoint)",
        "81015": "API key invalida o restringida (IP/modelo/endpoint)",
        "81009": "Modelo invalido",
        "81016": "Modelo no disponible en esta URL de API",
        "81101": "Limite QPS/RPM excedido (429)",
        "81114": "Limite TPM excedido (429)",
        "81116": "Proteccion por escalado rapido de trafico (429)",
        "81112": "Recurso no disponible (posible adeudo en la cuenta)",
        "81001": "Parametros invalidos en la peticion",
    }

    def __init__(self, status: int, body: str = ""):
        self.status = status
        self.body = body[:300]
        m = re.search(r"ModelArts\.(\d{5})", body or "")
        self.code = m.group(1) if m else None
        super().__init__(self.friendly())

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504)

    def friendly(self) -> str:
        if self.code in self.FRIENDLY:
            return f"{self.FRIENDLY[self.code]} [HTTP {self.status} · {self.code}]"
        hints = {401: "API key invalida", 403: "Acceso denegado", 404: "Modelo o ruta no encontrados"}
        return f"{hints.get(self.status, 'Error MaaS')} [HTTP {self.status}] {self.body[:120]}"


def parse_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("La respuesta no contiene JSON: " + t[:80])
    d = json.loads(m.group(0))
    ev = str(d.get("event", NORMAL)).upper().replace(" ", "_")
    if ev not in LABELS:
        ev = NORMAL
    events = [str(e).upper().replace(" ", "_") for e in (d.get("events") or [ev])]
    events = [e for e in events if e in LABELS] or [ev]
    person_down = bool(d.get("person_down", False))
    raw_flags = d.get("flags") or []
    if isinstance(raw_flags, str):
        raw_flags = [raw_flags]
    flags = [f for f in dict.fromkeys(str(x).lower().strip().replace(" ", "_") for x in raw_flags) if f in FLAGS]
    if person_down and ev == NORMAL:  # incoherente: persona caida no es trafico normal
        ev = "PEDESTRIAN_ON_ROAD"
    if flags and ev == NORMAL:        # bandera sin otro evento: se registra como conducta de riesgo
        ev = "RISK_BEHAVIOR"
    if ev not in events:
        events.insert(0, ev)
    if ev != NORMAL:
        events = [e for e in events if e != NORMAL]
    try:
        conf = float(d.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 1:
        conf /= 100
    sev = str(d.get("severity", "low")).lower()
    return {"event": ev, "events": events, "person_down": person_down, "flags": flags,
            "confidence": max(0.0, min(conf, 1.0)), "severity": sev if sev in SEV else "low",
            "reason": str(d.get("reason", ""))[:140], "scene": str(d.get("scene", ""))[:320]}


async def _call_once(client: httpx.AsyncClient, frames_b64: list[str], describe: bool, hint: str = "") -> tuple[dict, dict]:
    content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in frames_b64]
    content.append({"type": "text", "text": "Clasifica la ultima imagen." + (" " + hint if hint else "")})
    body = {
        "model": MAAS_MODEL,
        "messages": [{"role": "system", "content": system_prompt(describe)}, {"role": "user", "content": content}],
        "thinking": {"type": "disabled"},
        "max_tokens": MAX_TOKENS + (120 if describe else 0),
        "temperature": 0.1,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {MAAS_API_KEY}", "Content-Type": "application/json"}
    await rpm_wait()
    t0 = time.perf_counter()
    r = await client.post(MAAS_URL, json=body, headers=headers, timeout=TIMEOUT_S)
    http_ms = round((time.perf_counter() - t0) * 1000)
    if r.status_code != 200:
        raise MaasError(r.status_code, r.text)
    data = r.json()
    choice = data["choices"][0]
    text = choice["message"].get("content") or ""
    if not text.strip():
        raise ValueError("Respuesta vacia (¿thinking activo o max_tokens muy bajo?)")
    usage = data.get("usage") or {}
    try:
        res = parse_json(text)
    except Exception as e:  # noqa: BLE001
        log.info("PARSE-ERROR raw=%r", text[:400])
        raise ValueError(f"No se pudo leer la respuesta del modelo: {e}") from None
    return res, {"http_ms": http_ms, "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                 "tokens_total": usage.get("total_tokens"),
                 "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                 "finish": choice.get("finish_reason"), "raw": text[:1500], "describe": describe}


async def call_maas(client: httpx.AsyncClient, frames_b64: list[str], describe: bool = False, hint: str = "") -> tuple[dict, dict]:
    """Una llamada con un reintento rapido ante 429/5xx/timeout."""
    for attempt in range(2):
        try:
            res, meta = await _call_once(client, frames_b64, describe, hint)
            meta["retried"] = attempt > 0
            return res, meta
        except MaasError as e:
            if e.retryable and attempt == 0:
                await asyncio.sleep(0.6)
                continue
            raise
        except httpx.TimeoutException:
            if attempt == 0:
                continue
            raise RuntimeError(f"Timeout de MaaS (> {TIMEOUT_S:.0f} s, 2 intentos)") from None
        except httpx.HTTPError as e:
            if attempt == 0:
                await asyncio.sleep(0.6)
                continue
            raise RuntimeError(f"Error de red hacia MaaS: {type(e).__name__}") from None
    raise RuntimeError("Sin respuesta")


async def call_mock(idx: int, describe: bool = False) -> tuple[dict, dict]:
    await asyncio.sleep(random.uniform(1.6, 3.8) + (0.5 if describe else 0))
    script = [NORMAL, NORMAL, "TRAFFIC_CONGESTION", "STOPPED_VEHICLE", "COLLISION", "COLLISION",
              "FIRE_OR_SMOKE", "PEDESTRIAN_ON_ROAD", "FIGHT_OR_VIOLENCE", "ASSAULT_OR_ROBBERY",
              "PROTEST_OR_BLOCKADE", "CROWD_GATHERING", "PEDESTRIAN_STRUCK", "WRONG_WAY_VEHICLE",
              "RISK_BEHAVIOR", "VANDALISM_OR_DAMAGE", "CAMERA_OBSTRUCTED", NORMAL]
    ev = script[idx % len(script)]
    sev = {"COLLISION": "high", "FIRE_OR_SMOKE": "high", "PEDESTRIAN_ON_ROAD": "medium",
                                      "ASSAULT_OR_ROBBERY": "high", "FIGHT_OR_VIOLENCE": "high",
                                      "PROTEST_OR_BLOCKADE": "medium", "PEDESTRIAN_STRUCK": "high",
                                      "WRONG_WAY_VEHICLE": "high", "RISK_BEHAVIOR": "medium",
                                      "VANDALISM_OR_DAMAGE": "medium"}.get(ev, "low")
    res = {"event": ev, "events": [ev], "person_down": ev == "COLLISION" and idx % 2 == 0,
           "flags": ["armed_person"] if ev == "RISK_BEHAVIOR" else [],
           "confidence": round(random.uniform(0.62, 0.95), 2), "severity": sev,
           "reason": i18n.pick("resultado simulado (modo MOCK)", "simulated result (MOCK mode)"),
           "scene": i18n.pick("Descripcion simulada de la escena (modo MOCK).", "Simulated scene description (MOCK mode).") if describe else ""}
    tin, tout = random.randint(1300, 1900), random.randint(60, 90) + (35 if describe else 0)
    return res, {"http_ms": 0, "tokens_in": tin, "tokens_out": tout, "tokens_total": tin + tout, "cached": 0,
                 "finish": "stop", "raw": json.dumps(res, ensure_ascii=False), "describe": describe, "retried": False}


# ---- Analisis de resultados ---------------------------------------------------------
def latency_stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "p50": None, "p95": None, "max": None, "in_budget_pct": None}
    s = sorted(vals)
    p95 = s[min(len(s) - 1, max(0, -(-95 * len(s) // 100) - 1))]
    return {"n": len(s), "p50": round(statistics.median(s)), "p95": round(p95), "max": round(s[-1]),
            "in_budget_pct": round(100 * sum(v <= BUDGET_MS for v in s) / len(s))}


def token_stats(results: list[dict], duration: float) -> dict:
    ok = [r for r in results if not r.get("error") and r.get("tokens_in") is not None]
    ti = sum(r["tokens_in"] for r in ok)
    to = sum(r.get("tokens_out") or 0 for r in ok)
    n = len(ok)
    total = ti + to
    return {"frames": n, "in": ti, "out": to, "total": total, "cached": sum(r.get("cached") or 0 for r in ok),
            "avg_in": round(ti / n) if n else None, "avg_out": round(to / n) if n else None,
            "images_kb": sum(r.get("kb") or 0 for r in results),
            "per_video_minute": round(total / duration * 60) if duration and n else None}


def group_incidents(results: dict[int, dict]) -> list[dict]:
    """Une frames consecutivos con el mismo evento en un solo incidente."""
    out: list[dict] = []
    cur = None
    if not results:
        return out
    for i in range(max(results) + 1):
        r = results.get(i)
        if r is None:          # frame aun en analisis: corta el incidente
            cur = None
            continue
        if r.get("error"):     # frame con error: no corta ni suma
            continue
        if r["event"] == NORMAL and not r["person_down"]:
            cur = None
            continue
        if cur and cur["event"] == r["event"]:
            cur["t_end"] = r["t"]
            cur["n"] += 1
        else:
            cur = {"id": i, "event": r["event"], "events": [], "t_start": r["t"], "t_end": r["t"], "n": 1,
                   "confidence": 0.0, "severity": "low", "person_down": False, "flags": [], "best_idx": i, "reason": ""}
            out.append(cur)
        for e in r["events"]:
            if e != NORMAL and e not in cur["events"]:
                cur["events"].append(e)
        cur["person_down"] |= r["person_down"]
        for f in r.get("flags", []):
            if f not in cur["flags"]:
                cur["flags"].append(f)
        if SEV[r["severity"]] > SEV[cur["severity"]]:
            cur["severity"] = r["severity"]
        if r["confidence"] >= cur["confidence"]:
            cur["confidence"], cur["best_idx"], cur["reason"] = r["confidence"], i, r["reason"]
    return out


def evaluate_case(expected: list[str], expect_person_down: bool, results: list[dict],
                  expected_flags: list[str] | None = None, expected_analytics: list[str] | None = None,
                  detected_analytics: set[str] | None = None) -> dict:
    """Compara lo esperado con lo detectado (solo detecciones con confianza >= MIN_CONF)."""
    exp = {e for e in expected if e != NORMAL}
    ok = [r for r in results if not r.get("error")]
    det = {e for r in ok if r["confidence"] >= MIN_CONF for e in r["events"] if e != NORMAL}
    pd = any(r["person_down"] and r["confidence"] >= MIN_CONF for r in ok)
    xf = set(expected_flags or [])
    dflags = {f for r in ok if r["confidence"] >= MIN_CONF for f in r.get("flags", [])}
    missed = sorted(exp - det) + sorted(xf - dflags)
    extra = sorted(det - exp) + sorted(dflags - xf)
    if expect_person_down and not pd:
        missed.append("person_down")
    if not exp and not expect_person_down and not xf:
        status = "pass" if not det and not pd and not dflags else "fail"
        extra = sorted(det) + sorted(dflags) + (["person_down"] if pd else [])
    elif missed:
        status = "fail"
    elif extra:
        status = "partial"
    else:
        status = "pass"
    da = detected_analytics or set()
    am = [a for a in (expected_analytics or [])
          if not any((w.startswith(a[:-1]) if a.endswith("*") else w == a) for w in da)]
    if am:
        status = "fail"
        missed = missed + am
    return {"status": status, "expected": sorted(exp), "expected_person_down": expect_person_down,
            "expected_analytics": expected_analytics or [], "detected_analytics": sorted(da),
            "expected_flags": sorted(xf), "detected_flags": sorted(dflags),
            "detected": sorted(det), "detected_person_down": pd, "missed": missed, "extra": extra,
            "frames_ok": len(ok), "frames_error": len(results) - len(ok)}


# ---- Pipeline ---------------------------------------------------------------------
async def pipeline(path: Path, emit, speed: float = 1.0, context_frames: int | None = None,
                   describe: bool | None = None, log_dir: Path | None = None, tag: str = "-",
                   use_yolo: bool | None = None, yolo_hints: bool | None = None, info: dict | None = None,
                   zones: dict | None = None) -> list[dict]:
    """Captura frames y los analiza en paralelo. emit(evento) recibe cada evento. Devuelve los resultados."""
    ctx = CONTEXT_FRAMES if context_frames is None else context_frames
    desc = DESCRIBE if describe is None else describe
    yolo = YOLO_ON if use_yolo is None else (bool(use_yolo) and detector.available())
    hints_on = YOLO_HINTS if yolo_hints is None else bool(yolo_hints)
    info = info if info is not None else {}
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError("No se pudo abrir el video (formato/codec no soportado)")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = total_frames / fps
    crop = detect_crop(cap, total_frames) if AUTOCROP and total_frames > 3 else None
    progress = {"pct": 0.0}
    ysamples: list = []
    ysec = 0.0
    motion: list = []
    qsamples: list = []
    fsamples: list = []
    for attempt in range(2):
        scan = asyncio.create_task(asyncio.to_thread(scan_video, cap, fps, crop, yolo, progress))
        while not scan.done():
            await asyncio.sleep(0.4)
            await emit({"type": "scan", "pct": round(progress["pct"] * 100), "yolo": yolo})
        try:
            motion, ysamples, ysec, qsamples, fsamples = scan.result()
            break
        except Exception as e:  # noqa: BLE001
            if yolo and attempt == 0:      # YOLO fallo (pesos, memoria...): seguir sin el
                log.info("job=%s YOLO ERROR %s: %s", tag, type(e).__name__, e)
                await emit({"type": "warn", "message": i18n.pick(f"YOLO no disponible ({type(e).__name__}: {str(e)[:160]}). Se continua sin YOLO.", f"YOLO unavailable ({type(e).__name__}: {str(e)[:160]}). Continuing without YOLO.")})
                yolo, progress["pct"] = False, 0.0
                continue
            raise
    boxes, triggers = [], []
    flow: dict | None = None
    yinfo = None
    zones = zones or {}
    if yolo and ysamples:
        an = await asyncio.to_thread(detector.analyze, ysamples, zones)
        boxes, triggers, flow = an["boxes"], an["triggers"], an["flow"]
        yinfo = {"model": detector.YOLO_WEIGHTS, "samples": len(ysamples), "seconds": round(ysec, 1),
                 "ms_per_sample": round(1000 * ysec / max(1, len(ysamples))), "triggers": len(triggers)}
        keep = boxes if len(boxes) <= 4000 else boxes[:: -(-len(boxes) // 4000)]
        await emit({"type": "yolo", "classes": detector.CLASSES, "boxes": keep, "triggers": triggers, "info": yinfo,
                    "zones": zones})
        log.info("job=%s YOLO %s muestras=%d %.1fs (%d ms/muestra) disparadores=%d", tag, detector.YOLO_WEIGHTS,
                 len(ysamples), ysec, yinfo["ms_per_sample"], len(triggers))
    cam_events = await asyncio.to_thread(quality.analyze, qsamples) if QUALITY_ON else []
    cam_events = cam_events + (await asyncio.to_thread(fire.analyze, fsamples, i18n.en()) if fire.ON else [])
    events_all = sorted(triggers + cam_events, key=lambda e: e["t"])       # registro de analitica (YOLO + camara + fuego)
    analytics = {"events": events_all, "flow": flow, "zones": zones,
                 "counts": {k: sum(1 for e in events_all if e["why"] == k) for k in sorted({e["why"] for e in events_all})}}
    await emit({"type": "analytics", **analytics})
    for e in events_all:
        log.info("job=%s ANALITICA t=%.1fs %s%s", tag, e["t"], e["why"], f" ({e['detail']})" if e.get("detail") else "")
    if flow:
        log.info("job=%s FLUJO veh_unicos=%s pers_unicas=%s veh/min=%s ocup=%s vel=%s tendencia=%s estado=%s", tag,
                 flow["unique_vehicles"], flow["unique_persons"], flow["vehicles_per_min"], flow["occupancy_mean"],
                 flow["speed_mean"], flow["trend"], flow["state"])
    plan = plan_times(duration, motion, events_all)
    info.update({"yolo": yinfo, "triggers": triggers, "boxes": boxes, "plan": plan, "analytics": analytics})
    await emit({"type": "start", "duration": duration, "mock": MOCK, "model": MAAS_MODEL,
                "interval": FRAME_INTERVAL, "budget_ms": BUDGET_MS, "n_frames": len(plan),
                "crop": list(crop) if crop else None, "extra": sum(1 for _, w in plan if w != "base"),
                "yolo": bool(yinfo), "hints": bool(hints_on and (yinfo or cam_events)),
                "motion": [[round(t, 2), round(m, 2)] for t, m in motion[:: max(1, len(motion) // 300)]]})

    results: dict[int, dict] = {}
    meta_by_idx: dict[int, dict] = {}
    work: asyncio.Queue = asyncio.Queue()

    async def finish(idx: int, r: dict):
        r = {**meta_by_idx.get(idx, {}), **r}
        results[idx] = r
        if r.get("error"):
            log.info("job=%s #%03d t=%.1fs ERROR %s", tag, idx, r["t"], r["error"])
        else:
            log.info("job=%s #%03d t=%.1fs (%s) %d img %dKB | http=%dms total=%dms cola=%dms | tok %s->%s cache=%s | %s %.2f %s%s%s",
                     tag, idx, r["t"], r.get("why", "?"), r.get("frames_sent", 0), r.get("kb", 0), r.get("http_ms", 0),
                     r["total_ms"], r.get("queue_ms", 0), r.get("tokens_in"), r.get("tokens_out"), r.get("cached"),
                     r["event"], r["confidence"], r["severity"], (" PERSONA_CAIDA" if r["person_down"] else "") + (" FLAGS=" + ",".join(r["flags"]) if r.get("flags") else ""),
                     f" | escena: {r['scene']}" if r.get("scene") else "")
        if log_dir:
            with open(log_dir / "log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        await emit({"type": "error" if r.get("error") else "result", **r})
        await emit({"type": "incidents", "items": group_incidents(results)})

    async def worker(client):
        while True:
            item = await work.get()
            if item is None:
                work.task_done()
                return
            idx, t_video, frames, t_capture, hint = item
            t_start = time.perf_counter()
            queue_ms = round((t_start - t_capture) * 1000)
            try:
                res, meta = await (call_mock(idx, desc) if MOCK else call_maas(client, frames, desc, hint))
                total_ms = round((time.perf_counter() - t_capture) * 1000)
                await finish(idx, {"idx": idx, "t": t_video, "total_ms": total_ms, "queue_ms": queue_ms,
                                   "in_budget": total_ms <= BUDGET_MS, **res, **meta})
            except Exception as e:  # noqa: BLE001
                await finish(idx, {"idx": idx, "t": t_video, "error": str(e)[:220],
                                   "total_ms": round((time.perf_counter() - t_capture) * 1000),
                                   "queue_ms": queue_ms})
            finally:
                work.task_done()

    limits = httpx.Limits(max_connections=WORKERS + 2, max_keepalive_connections=WORKERS + 2)
    async with httpx.AsyncClient(limits=limits, http2=False) as client:
        if not MOCK:  # calienta la conexion TLS para que el primer frame no pague el handshake
            try:
                await client.get(MODELS_URL, headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=4)
            except Exception:  # noqa: BLE001
                pass
        workers = [asyncio.create_task(worker(client)) for _ in range(WORKERS)]
        clock = time.perf_counter()

        def build(t_video: float):
            cur = grab_at(cap, t_video, crop)
            if cur is None:
                return None
            olds = [grab_at(cap, t_video - k * CONTEXT_GAP_S, crop) for k in range(ctx, 0, -1)
                    if t_video - k * CONTEXT_GAP_S >= 0]
            imgs = [i for i in olds if i is not None] + [cur]
            return [encode_frame(i, FRAME_WIDTH, JPEG_QUALITY) for i in imgs], encode_frame(cur, 320, 60)

        for idx, (t_video, why) in enumerate(plan):
            wait = t_video / speed - (time.perf_counter() - clock)
            if wait > 0:
                await asyncio.sleep(wait)
            t_capture = time.perf_counter()
            built = await asyncio.to_thread(build, t_video)
            if built is None:
                continue
            frames, thumb = built
            kb = round(sum(len(f) for f in frames) * 3 / 4 / 1024)
            files: list[str] = []
            if log_dir:
                for k, fb in enumerate(frames):
                    fn = f"{idx:03d}_{k}.jpg"
                    (log_dir / fn).write_bytes(base64.b64decode(fb))
                    files.append(fn)
            hint = detector.hint_for(boxes, events_all, t_video) if (hints_on and (yolo or cam_events)) else ""
            meta_by_idx[idx] = {"why": why, "kb": kb, "frames_sent": len(frames), "files": files, "hint": hint}
            await emit({"type": "capture", "idx": idx, "t": t_video, "why": why, "kb": kb,
                        "frames_sent": len(frames), "thumb": thumb, "files": files, "hint": hint})
            await work.put((idx, t_video, frames, t_capture, hint))
        await work.join()
        for _ in workers:
            await work.put(None)
        await asyncio.gather(*workers)
    cap.release()
    return [results[i] for i in sorted(results)]


# ---- Pipeline en vivo (RTSP / RTMP) ---------------------------------------------------------
PREVIEW_COL = {"person": (0, 177, 242), "bicycle": (0, 177, 242), "backpack": (128, 114, 107), "handbag": (128, 114, 107),
               "suitcase": (128, 114, 107)}


def render_preview(img, dets, width: int = 640) -> bytes:
    """Frame reducido con las cajas de YOLO dibujadas, en JPEG, para el visor en vivo (MJPEG)."""
    h, w = img.shape[:2]
    im = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA) if w > width else img.copy()
    H, W = im.shape[:2]
    for d in dets or []:
        col = PREVIEW_COL.get(d["c"], (55, 41, 31))
        p1, p2 = (int(d["x1"] * W), int(d["y1"] * H)), (int(d["x2"] * W), int(d["y2"] * H))
        cv2.rectangle(im, p1, p2, col, 2)
        cv2.putText(im, d["c"], (p1[0] + 2, max(12, p1[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 65])
    return buf.tobytes() if ok else b""


def prune_frames(log_dir: Path):
    cut = time.time() - CAM_LOG_KEEP_MIN * 60
    for f in log_dir.glob("*.jpg"):
        try:
            if f.stat().st_mtime < cut:
                f.unlink()
        except OSError:
            pass


async def live_pipeline(job: dict, emit, log_dir: Path | None, tag: str, describe: bool | None, use_yolo: bool | None,
                        yolo_hints: bool | None, info: dict, zones: dict | None) -> list[dict]:
    """Analiza un stream en vivo: YOLO + calidad de imagen corren sin parar en la ECS; un frame cada FRAME_INTERVAL
    (mas los extras por alerta o movimiento) viaja a MaaS. Termina cuando job['stop'] se activa."""
    ctx = CONTEXT_FRAMES
    desc = DESCRIBE if describe is None else describe
    S = {"yolo": (YOLO_ON if use_yolo is None else bool(use_yolo)) and detector.available(), "ysec": 0.0, "ysteps": 0,
         "boxes": [], "flow": None, "dets": [], "last_an": -1e9}
    hints_on = YOLO_HINTS if yolo_hints is None else bool(yolo_hints)
    zones = zones or {}
    stop: asyncio.Event = job["stop"]
    src = live.LiveSource(job["url"], ring_width=FRAME_WIDTH)
    job["live_src"] = src
    src.start()
    t_wait = time.perf_counter()
    while src.latest() is None:
        if stop.is_set():
            src.stop()
            return []
        if time.perf_counter() - t_wait > LIVE_CONNECT_S:
            src.stop()
            raise RuntimeError(i18n.pick(
                f"No llego video de {job['name']} en {LIVE_CONNECT_S:.0f} s. Revisa que la fuente este publicando, la URL y los puertos abiertos.",
                f"No video arrived from {job['name']} within {LIVE_CONNECT_S:.0f} s. Check that the source is publishing, the URL and the open ports."))
        await asyncio.sleep(0.2)
    await emit({"type": "start", "live": True, "duration": 120.0, "mock": MOCK, "model": MAAS_MODEL,
                "interval": FRAME_INTERVAL, "budget_ms": BUDGET_MS, "n_frames": 0, "crop": None, "extra": 0,
                "yolo": bool(S["yolo"]), "hints": bool(hints_on and (S["yolo"] or QUALITY_ON)), "motion": [],
                "size": list(src.size) if src.size else None})
    log.info("job=%s LIVE conectado %s %s", tag, job["name"], src.size)

    ysamples: list = []
    qsamples: list = []
    fsamples: list = []
    motion: list = []
    events_all: list[dict] = []
    last_ev: dict[str, float] = {}
    extras: list[tuple[float, str]] = []
    results: dict[int, dict] = {}
    meta_by_idx: dict[int, dict] = {}
    work: asyncio.Queue = asyncio.Queue()

    async def window_analysis():
        trig: list[dict] = []
        if S["yolo"] and ysamples:
            an = await asyncio.to_thread(detector.analyze, list(ysamples), zones)
            S["boxes"], S["flow"], trig = an["boxes"], an["flow"], an["triggers"]
        cam = await asyncio.to_thread(quality.analyze, list(qsamples)) if QUALITY_ON else []
        if fire.ON:
            cam = cam + await asyncio.to_thread(fire.analyze, list(fsamples), i18n.en())
        fresh = False
        for ev in sorted(trig + cam, key=lambda e: e["t"]):
            if ev["t"] <= last_ev.get(ev["why"], -1e9) + LIVE_COOLDOWN.get(ev["why"], LIVE_COOLDOWN_DEFAULT):
                continue
            last_ev[ev["why"]] = ev["t"]
            events_all.append(ev)
            extras.append((ev["t"] + 0.3, ev["why"]))
            fresh = True
            log.info("job=%s ANALITICA t=%.1fs %s%s", tag, ev["t"], ev["why"], f" ({ev['detail']})" if ev.get("detail") else "")
        now = src.now()
        if fresh or now - S["last_an"] > 4:
            S["last_an"] = now
            await emit({"type": "analytics", "events": events_all, "flow": S["flow"], "zones": zones,
                        "counts": {k: sum(1 for e in events_all if e["why"] == k) for k in sorted({e["why"] for e in events_all})}})

    async def analysis_loop():
        prev, fprev, last_t, next_win = None, None, -1.0, 0.0
        while not stop.is_set():
            lat = src.latest()
            if lat is None or lat[0] <= last_t:
                await asyncio.sleep(0.05)
                continue
            t, img = lat
            last_t = t
            t_loop = time.perf_counter()

            def step(prev_g=prev, prev_f=fprev):
                g, qm = quality.metrics(img, prev_g)
                fst, fm = fire.metrics(img, prev_f) if fire.ON else (None, None)
                dets, ms = None, 0.0
                if S["yolo"]:
                    im = img
                    if img.shape[1] > 1280:
                        im = cv2.resize(img, (1280, int(img.shape[0] * 1280 / img.shape[1])), interpolation=cv2.INTER_AREA)
                    t0 = time.perf_counter()
                    dets = detector.detect([im])[0]
                    ms = (time.perf_counter() - t0) * 1000
                return g, qm, dets, ms, fst, fm

            try:
                g, qm, dets, ms, fst, fm = await asyncio.to_thread(step)
            except Exception as e:  # noqa: BLE001
                if not S["yolo"]:
                    raise
                S["yolo"] = False
                log.info("job=%s YOLO ERROR %s: %s", tag, type(e).__name__, e)
                await emit({"type": "warn", "message": i18n.pick(
                    f"YOLO no disponible ({type(e).__name__}: {str(e)[:160]}). Se continua sin YOLO.",
                    f"YOLO unavailable ({type(e).__name__}: {str(e)[:160]}). Continuing without YOLO.")})
                continue
            if prev is not None:
                motion.append((t, qm["diff"]))
            if QUALITY_ON:
                qsamples.append((t, qm))
            if fm is not None:
                fsamples.append((t, fm))
                fprev = fst
            prev = g
            if dets is not None:
                ysamples.append((t, dets))
                S["dets"] = dets
                job["live_dets"] = dets
                S["ysec"] += ms / 1000
                S["ysteps"] += 1
            cut = t - LIVE_WINDOW_S
            while ysamples and ysamples[0][0] < cut:
                ysamples.pop(0)
            while qsamples and qsamples[0][0] < cut:
                qsamples.pop(0)
            while fsamples and fsamples[0][0] < cut:
                fsamples.pop(0)
            del motion[:-240]
            if ADAPTIVE and len(motion) >= 20:       # pico de movimiento: la muestra anterior es maximo local
                vals = np.array([m for _, m in motion])
                med = float(np.median(vals))
                mad = float(np.median(np.abs(vals - med))) * 1.4826 or 0.5
                thr = max(med + MOTION_SIGMA * mad, med * 1.8, 1.5)
                if motion[-2][1] >= thr and motion[-2][1] >= motion[-3][1] and motion[-2][1] >= motion[-1][1]:
                    extras.append((motion[-2][0] + 0.5, "movimiento"))
            if t >= next_win:
                next_win = t + LIVE_ANALYZE_EVERY
                await window_analysis()
            iv = 1.0 / max(0.5, detector.YOLO_LIVE_FPS if S["yolo"] else SCAN_FPS)
            await asyncio.sleep(max(0.0, iv - (time.perf_counter() - t_loop)))

    async def finish(idx: int, r: dict):
        r = {**meta_by_idx.get(idx, {}), **r}
        results[idx] = r
        if r.get("error"):
            log.info("job=%s #%03d t=%.1fs ERROR %s", tag, idx, r["t"], r["error"])
        else:
            log.info("job=%s #%03d t=%.1fs (%s) %d img %dKB | http=%dms total=%dms cola=%dms | tok %s->%s | %s %.2f %s%s",
                     tag, idx, r["t"], r.get("why", "?"), r.get("frames_sent", 0), r.get("kb", 0), r.get("http_ms", 0),
                     r["total_ms"], r.get("queue_ms", 0), r.get("tokens_in"), r.get("tokens_out"),
                     r["event"], r["confidence"], r["severity"],
                     (" PERSONA_CAIDA" if r["person_down"] else "") + (" FLAGS=" + ",".join(r["flags"]) if r.get("flags") else ""))
        if log_dir:
            with open(log_dir / "log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        await emit({"type": "error" if r.get("error") else "result", **r})
        await emit({"type": "incidents", "items": group_incidents(results)})

    async def worker(client):
        while True:
            item = await work.get()
            if item is None:
                work.task_done()
                return
            idx, t_video, frames, t_capture, hint = item
            queue_ms = round((time.perf_counter() - t_capture) * 1000)
            try:
                res, meta = await (call_mock(idx, desc) if MOCK else call_maas(client, frames, desc, hint))
                total_ms = round((time.perf_counter() - t_capture) * 1000)
                await finish(idx, {"idx": idx, "t": t_video, "total_ms": total_ms, "queue_ms": queue_ms,
                                   "in_budget": total_ms <= BUDGET_MS, **res, **meta})
            except Exception as e:  # noqa: BLE001
                await finish(idx, {"idx": idx, "t": t_video, "error": str(e)[:220],
                                   "total_ms": round((time.perf_counter() - t_capture) * 1000), "queue_ms": queue_ms})
            finally:
                work.task_done()

    def build(t: float, img):
        olds = []
        for k in range(ctx, 0, -1):
            r = src.ring_at(t - k * CONTEXT_GAP_S)
            if r and t - r[0] <= k * CONTEXT_GAP_S + 1.0:
                olds.append(r[1])
        imgs = olds + [img]
        return [encode_frame(i, FRAME_WIDTH, JPEG_QUALITY) for i in imgs], encode_frame(img, 320, 60)

    async def capture_loop():
        idx, next_base, last_cap = 0, 0.0, -1e9
        extra_times: list[float] = []
        while not stop.is_set():
            lat = src.latest()
            now = src.now()
            why = None
            if lat is not None and now - lat[0] < 3.0:        # solo si el stream esta llegando
                if now >= next_base:
                    why = "base"
                    next_base = now + FRAME_INTERVAL if now - next_base > FRAME_INTERVAL else next_base + FRAME_INTERVAL
                else:
                    while extras and extras[0][0] < now - 5:
                        extras.pop(0)
                    extra_times[:] = [x for x in extra_times if x > now - 60]
                    ready = [e for e in extras if e[0] <= now]
                    if ready and now - last_cap >= MIN_GAP_S and len(extra_times) < MAX_EXTRA_PER_MIN:
                        why = ready[0][1]
                        for e in ready:
                            extras.remove(e)
                        extra_times.append(now)
            if why:
                last_cap = now
                t_capture = time.perf_counter()
                t_video, img = lat
                frames, thumb = await asyncio.to_thread(build, t_video, img)
                kb = round(sum(len(f) for f in frames) * 3 / 4 / 1024)
                files: list[str] = []
                if log_dir:
                    for k, fb in enumerate(frames):
                        fn = f"{idx:03d}_{k}.jpg"
                        (log_dir / fn).write_bytes(base64.b64decode(fb))
                        files.append(fn)
                hint = detector.hint_for(S["boxes"], events_all, t_video) if (hints_on and (S["yolo"] or events_all)) else ""
                meta_by_idx[idx] = {"why": why, "kb": kb, "frames_sent": len(frames), "files": files, "hint": hint}
                await emit({"type": "capture", "idx": idx, "t": round(t_video, 2), "why": why, "kb": kb,
                            "frames_sent": len(frames), "thumb": thumb, "files": files, "hint": hint})
                await work.put((idx, round(t_video, 2), frames, t_capture, hint))
                idx += 1
            await asyncio.sleep(0.1)

    async def heartbeat():
        while not stop.is_set():
            lat = src.latest()
            age = src.now() - lat[0] if lat else 1e9
            max_min = job.get("max_min", LIVE_MAX_MIN)
            if job.get("cam") and log_dir and time.time() - job.get("pruned", 0) > 120:
                job["pruned"] = time.time()
                await asyncio.to_thread(prune_frames, log_dir)
            if src.now() > max_min * 60:
                await emit({"type": "warn", "message": i18n.pick(f"Se alcanzo el limite de {max_min:.0f} min por sesion. Sesion detenida.",
                                                                f"Session limit of {max_min:.0f} min reached. Session stopped.")})
                stop.set()
                return
            if age > 120:
                await emit({"type": "warn", "message": i18n.pick("El stream lleva 2 min sin enviar video. Sesion detenida.",
                                                                "The stream has sent no video for 2 min. Session stopped.")})
                stop.set()
                return
            await emit({"type": "live", "t": round(src.now(), 1), "status": src.status, "fps": src.fps(),
                        "reconnects": src.reconnects, "queue": work.qsize(), "frames": src.frames,
                        "yolo_ms": round(1000 * S["ysec"] / max(1, S["ysteps"])), "size": list(src.size) if src.size else None,
                        "age": round(age, 1) if age < 1e8 else None})
            await asyncio.sleep(1.0)

    err = None
    limits = httpx.Limits(max_connections=WORKERS + 2, max_keepalive_connections=WORKERS + 2)
    async with httpx.AsyncClient(limits=limits, http2=False) as client:
        if not MOCK:
            try:
                await client.get(MODELS_URL, headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=4)
            except Exception:  # noqa: BLE001
                pass
        workers = [asyncio.create_task(worker(client)) for _ in range(WORKERS)]
        tasks = [asyncio.create_task(f()) for f in (analysis_loop, capture_loop, heartbeat)]
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            if d is not stopper and not d.cancelled() and d.exception():
                err = d.exception()
        stop.set()
        for t in [*tasks, stopper]:
            t.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        await work.join()
        for _ in workers:
            await work.put(None)
        await asyncio.gather(*workers)
    src.stop()
    elapsed = src.now()
    if err:
        log.info("job=%s LIVE ERROR %s: %s", tag, type(err).__name__, err)
        await emit({"type": "fatal", "message": f"{type(err).__name__}: {str(err)[:200]}"})
    yinfo = ({"model": detector.YOLO_WEIGHTS, "samples": S["ysteps"], "seconds": round(S["ysec"], 1),
              "ms_per_sample": round(1000 * S["ysec"] / max(1, S["ysteps"])),
              "triggers": sum(1 for e in events_all if e.get("src") != "cam")} if S["ysteps"] else None)
    info.update({"yolo": yinfo, "triggers": [e for e in events_all if e.get("src") != "cam"], "boxes": S["boxes"], "duration": elapsed,
                 "analytics": {"events": events_all, "flow": S["flow"], "zones": zones,
                               "counts": {k: sum(1 for e in events_all if e["why"] == k) for k in sorted({e["why"] for e in events_all})}}})
    return [results[i] for i in sorted(results)]


async def run_job(job_id: str):
    job = JOBS[job_id]
    i18n.LANG.set(job.get("lang", "es"))
    q: asyncio.Queue = job["events"]
    log_dir = LOGS / job_id
    log_dir.mkdir(exist_ok=True)
    desc = job["describe"]
    yolo_flag, hints_flag = job.get("yolo"), job.get("hints")
    (log_dir / "prompt.txt").write_text(system_prompt(desc), encoding="utf-8")
    (log_dir / "config.json").write_text(json.dumps({
        "video": job["name"], "model": MAAS_MODEL, "mock": MOCK, "frame_interval_s": FRAME_INTERVAL,
        "adaptive": ADAPTIVE, "context_frames": CONTEXT_FRAMES, "context_gap_s": CONTEXT_GAP_S,
        "frame_width": FRAME_WIDTH, "jpeg_quality": JPEG_QUALITY, "max_tokens": MAX_TOKENS, "describe": desc,
        "autocrop": AUTOCROP, "workers": WORKERS, "timeout_s": TIMEOUT_S, "yolo": yolo_flag, "yolo_hints": hints_flag,
        "quality": QUALITY_ON, "zones": job.get("zones"), "yolo_weights": detector.YOLO_WEIGHTS, "yolo_conf": detector.YOLO_CONF, "yolo_fps": detector.YOLO_FPS}, indent=2), encoding="utf-8")
    log.info("job=%s INICIO video=%r describe=%s modelo=%s mock=%s", job_id, job["name"], desc, MAAS_MODEL, MOCK)

    src_name = job.get("source") or job["name"]          # fuente que ven el webhook y el historial
    src_kind = job.get("kind", "camara")

    async def emit(ev):
        if ev["type"] == "start":
            job["duration"] = ev["duration"]
            ev = {**ev, "describe": desc, "job_id": job_id}
        if not job.get("headless"):               # trabajos de OBS corren sin visor: no acumular eventos en memoria
            await q.put(ev)
        cb = job.get("on_event")
        if cb:
            try:
                cb(ev)
            except Exception as e:  # noqa: BLE001
                log.info("job=%s on_event error %s", job_id, e)
        if webhook.enabled():                     # incidentes -> webhook (no bloquea el analisis)
            try:
                if ev["type"] == "result":
                    asyncio.create_task(asyncio.to_thread(webhook.send_result, src_name, job_id, ev, log_dir, src_kind))
                elif ev["type"] == "analytics":
                    asyncio.create_task(asyncio.to_thread(webhook.send_alerts, src_name, job_id, ev.get("events") or [], job.setdefault("hook_an", set()), src_kind))
            except Exception as e:  # noqa: BLE001
                log.info("job=%s WEBHOOK error %s", job_id, e)
        cam = job.get("cam") or job.get("hist_cam")
        if cam:                                   # bitacora persistente por fuente y por dia
            try:
                cam_name = job.get("hist_name") or job["name"]
                if ev["type"] in ("result", "error"):
                    job["last"] = {k: ev.get(k) for k in ("event", "confidence", "severity", "person_down", "flags", "t")} | {"at": time.time(), "error": ev.get("error")}
                    await asyncio.to_thread(camlog.persist_result, cam, cam_name, job_id, ev, log_dir, src_name, src_kind)
                elif ev["type"] == "analytics":
                    await asyncio.to_thread(camlog.persist_alerts, cam, cam_name, job_id, ev.get("events") or [], job.setdefault("logged_an", set()), src_name, src_kind)
            except Exception as e:  # noqa: BLE001
                log.info("job=%s BITACORA error %s: %s", job_id, type(e).__name__, e)

    results: list[dict] = []
    info: dict = {}
    try:
        if job.get("live"):
            results = await live_pipeline(job, emit, log_dir, job_id, desc, yolo_flag, hints_flag, info, job.get("zones"))
        else:
            results = await pipeline(job["path"], emit, speed=job["speed"], describe=desc, log_dir=log_dir, tag=job_id,
                                     use_yolo=yolo_flag, yolo_hints=hints_flag, info=info, zones=job.get("zones"))
    except Exception as e:  # noqa: BLE001
        log.info("job=%s FATAL %s", job_id, e)
        job["fatal"] = str(e)[:250]
        await emit({"type": "fatal", "message": str(e)[:250]})
    if info.get("duration"):
        job["duration"] = info["duration"]
    job["finished"] = True
    ok = [r for r in results if not r.get("error")]
    stats = latency_stats([r["total_ms"] for r in ok])
    tokens = token_stats(results, job.get("duration", 0))
    verdict = None
    if job.get("case"):
        c = job["case"]
        verdict = evaluate_case(c["expected"], c.get("person_down", False), results, c.get("flags"), c.get("analytics"),
                                {e["why"] for e in (info.get("analytics") or {}).get("events", [])})
    job["report"] = {"job_id": job_id, "video": job["name"], "source": src_name, "model": MAAS_MODEL, "mock": MOCK,
                     "fatal": job.get("fatal"), "yolo_error": detector.load_error(),
                     "interval_s": FRAME_INTERVAL, "context_frames": CONTEXT_FRAMES, "budget_ms": BUDGET_MS,
                     "describe": desc, "yolo": info.get("yolo"), "yolo_triggers": info.get("triggers", []), "analytics": info.get("analytics"),
                     "latency": stats, "tokens": tokens, "verdict": verdict, "results": results,
                     "incidents": group_incidents({r["idx"]: r for r in results})}
    if info.get("analytics"):
        (log_dir / "analytics.json").write_text(json.dumps(info["analytics"], ensure_ascii=False, indent=2), encoding="utf-8")
    if info.get("boxes"):
        (log_dir / "yolo.json").write_text(json.dumps({"info": info.get("yolo"), "triggers": info.get("triggers"),
                                                       "boxes": info["boxes"]}), encoding="utf-8")
    (log_dir / "report.json").write_text(json.dumps(job["report"], ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("job=%s FIN frames=%d p50=%sms p95=%sms tokens=%s (in %s / out %s) por_min_video=%s", job_id, stats["n"],
             stats["p50"], stats["p95"], tokens["total"], tokens["in"], tokens["out"], tokens["per_video_minute"])
    if job.get("obs"):
        await asyncio.to_thread(ingest_publish, job, log_dir)
    await emit({"type": "done", "stats": stats, "tokens": tokens, "verdict": verdict,
                "case": {k: job["case"][k] for k in ("id", "title", "expected", "person_down", "flags", "analytics") if k in job["case"]}
                if job.get("case") else None})


# ---- Casos de prueba -------------------------------------------------------------------
def parse_zone(raw: str | None) -> list[list[float]] | None:
    """'x,y;x,y;x,y' (coordenadas 0-1) -> poligono, o None si no es valido."""
    if not raw:
        return None
    try:
        pts = [[min(1.0, max(0.0, float(a))) for a in p.split(",")] for p in raw.split(";") if p.strip()]
    except ValueError:
        return None
    return pts if len(pts) >= 3 and all(len(p) == 2 for p in pts) else None


def load_cases() -> list[dict]:
    p = CASES_DIR / "casos.json"
    if not p.exists():
        return []
    cases = json.loads(p.read_text(encoding="utf-8"))
    for c in cases:
        c["available"] = (CASES_DIR / c["file"]).exists()
    return cases


def cleanup(max_age_h: float = 6):
    cutoff = time.time() - max_age_h * 3600
    for f in UPLOADS.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
    for d in LOGS.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < time.time() - 24 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    for jid in [j for j, v in JOBS.items() if v["created"] < cutoff and (v.get("finished") or not v.get("live"))]:
        JOBS.pop(jid, None)
    camlog.prune()


async def new_job(src: Path, name: str, case: dict | None = None) -> dict:
    playable = await asyncio.to_thread(prepare_video, src)
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"path": playable, "name": name, "case": case, "events": asyncio.Queue(), "speed": 1.0,
                 "describe": DESCRIBE, "yolo": YOLO_ON, "hints": YOLO_HINTS, "zones": (case or {}).get("zones") or {},
                 "started": False, "created": time.time(), "report": None}
    return {"job_id": jid, "video_url": f"/api/video/{jid}", "name": name}


# ---- API ----------------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"ok": True, "mock": MOCK}


@app.post("/api/webhook/test")
async def webhook_test():
    if not webhook.enabled():
        raise HTTPException(400, "WEBHOOK_URL no configurado en .env")
    ok, d = await asyncio.to_thread(webhook.post, webhook.test_payload())
    return {"ok": ok, "detail": d, "url": webhook.URL.split("?")[0]}


YOLO_STATE: dict = {"ok": None, "weights": None, "ms": None, "error": None}


async def _yolo_warmup():
    """Carga YOLO al arrancar para que un fallo (pesos que no se pueden bajar, falta de memoria...) se vea YA, no en el primer video."""
    r = await asyncio.to_thread(detector.warmup)
    YOLO_STATE.update(r)
    if r["ok"]:
        log.info("YOLO listo · %s · %s ms en la prueba · fuego/humo=%s", r["weights"], r["ms"], "si" if fire.ON else "no")
    else:
        log.error("YOLO NO CARGO (%s): %s. Sin YOLO se analiza solo con la cuadricula base y el VLM. Revisa models/ y la red.", r["weights"], r["error"])


@app.on_event("startup")
async def _startup_notes():
    if YOLO_ON:
        asyncio.create_task(_yolo_warmup())
    log.info("VIALERT %s · modelo=%s · %s · YOLO=%s · camaras=%d", VERSION, MAAS_MODEL, "SIMULADO (sin MAAS_API_KEY)" if MOCK else "MaaS real", detector.available(), len(CAMS))
    if not (DEMO_USER and DEMO_PASS):
        log.warning("SIN AUTENTICACION: define DEMO_USER y DEMO_PASS en .env si el puerto es alcanzable desde fuera.")
    if not publish_pass():
        log.info("MTX_PUBLISH_PASS no definido: el asistente de celular no podra mostrar la clave de publicacion.")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION, "mock": MOCK, "cams": len(CAMS), "running": sum(1 for c in CAMS.values() if cam_state(c)["running"])}


@app.get("/api/config")
async def config(lang: str = "es"):
    i18n.LANG.set("es")
    return {"mock": MOCK, "model": MAAS_MODEL, "interval": FRAME_INTERVAL, "labels": LABELS, "flags": FLAGS,
            "context_frames": CONTEXT_FRAMES, "context_gap": CONTEXT_GAP_S, "adaptive": ADAPTIVE,
            "budget_ms": BUDGET_MS, "describe": DESCRIBE, "yolo_available": detector.available(), "yolo": YOLO_ON,
            "yolo_ok": YOLO_STATE["ok"], "yolo_error": YOLO_STATE["error"], "fire": fire.ON,
            "yolo_hints": YOLO_HINTS, "quality": QUALITY_ON, "yolo_model": detector.YOLO_WEIGHTS, "prompt": system_prompt(False), "live": {"default_url": LIVE_URL, "max_min": LIVE_MAX_MIN}, "obs": {"configured": obsio.configured()}, "version": VERSION, "webhook": webhook.enabled(), "auth": bool(DEMO_USER and DEMO_PASS),
            "prompt_describe": system_prompt(True)}


def _mtx_local(url: str) -> str:
    """rtsp://127.0.0.1:8554/camN (o localhost) -> el MediaMTX real con la clave de lectura. En Docker, 127.0.0.1 es el
    propio contenedor de Vialert; asi el usuario escribe la URL corta y no hay que copiar ninguna clave."""
    u, m = urlsplit((url or "").strip()), urlsplit(MTX_READ_URL)
    if u.hostname in ("127.0.0.1", "localhost", "::1") and not u.username and m.hostname not in ("127.0.0.1", "localhost", "::1"):
        return f"{MTX_READ_URL}{u.path}" + (f"?{u.query}" if u.query else "")
    return url


@app.post("/api/live/start")
async def live_start(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        url = live.check_url(_mtx_local((body or {}).get("url") or LIVE_URL), en=(body or {}).get("lang") == "en")
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    jid = uuid.uuid4().hex[:10]
    name = live.safe_name(url)
    JOBS[jid] = {"live": True, "url": url, "path": None, "name": name, "case": None, "events": asyncio.Queue(),
                 "speed": 1.0, "describe": DESCRIBE, "yolo": YOLO_ON, "hints": YOLO_HINTS, "zones": {}, "started": False,
                 "created": time.time(), "report": None, "stop": asyncio.Event(), "finished": False}
    log.info("job=%s LIVE solicitado %s", jid, name)
    return {"job_id": jid, "name": name, "preview_url": f"/api/live/{jid}/mjpeg"}


@app.post("/api/live/{jid}/stop")
async def live_stop(jid: str):
    job = JOBS.get(jid)
    if not job or not job.get("live"):
        raise HTTPException(404)
    job["stop"].set()
    return {"ok": True}


@app.get("/api/live/{jid}/mjpeg")
async def live_mjpeg(jid: str):
    job = JOBS.get(jid)
    if not job or not job.get("live"):
        raise HTTPException(404)

    async def gen():
        while not job.get("finished"):
            src = job.get("live_src")
            lat = src.latest() if src else None
            if lat:
                jpg = await asyncio.to_thread(render_preview, lat[1], job.get("live_dets"))
                if jpg:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})




# ---- Camaras en vivo (alta/baja desde el front) + historial por camara y por dia ------------------
# Dos tipos de camara:
#   publish : el celular / codificador EMPUJA el video al servidor (MediaMTX); sirve detras de NAT o con 4G.
#             Vialert lee  MTX_READ_URL/<id>.
#   ip      : camara IP / NVR con RTSP; Vialert se conecta DIRECTO a su URL (red local, VPN o IP publica).
CAMS_FILE = camlog.EVENTS_DIR / "cameras.json"
CAM_STATE: dict = {"next": 1, "gone": {}}
_CAM_ID = re.compile(r"^cam\d{1,4}$")


def _guess_type(url: str) -> str:
    h = urlsplit(url).hostname or ""
    return "publish" if h in ("127.0.0.1", "localhost", "::1") or (h and h == urlsplit(MTX_READ_URL).hostname) else "ip"


def load_cams():
    try:
        raw = json.loads(CAMS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = {}
    if raw.get("v") == 2:
        items, CAM_STATE["next"], CAM_STATE["gone"] = raw.get("cams", {}), int(raw.get("next", 1)), raw.get("gone", {})
    else:                                       # formato anterior: {cam1: {name, url}, ...}
        items = {k: v for k, v in raw.items() if _CAM_ID.match(k) and isinstance(v, dict)}
        CAM_STATE["next"] = max([int(k[3:]) for k in items] + [0]) + 1
    for cid, sv in items.items():
        if not _CAM_ID.match(cid):
            continue
        typ = sv.get("type") or _guess_type(sv.get("url") or "")
        CAMS[cid] = {"id": cid, "name": sv.get("name") or cid.upper(), "type": typ,
                     "url": "" if typ == "publish" else (sv.get("url") or ""), "job_id": None}


def save_cams():
    data = {"v": 2, "next": CAM_STATE["next"], "gone": CAM_STATE["gone"],
            "cams": {k: {"name": v["name"], "type": v["type"], "url": v["url"]} for k, v in CAMS.items()}}
    try:
        tmp = CAMS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)                # puede contener usuario/clave de camaras IP
        except OSError:
            pass
        tmp.replace(CAMS_FILE)
    except OSError as e:
        log.info("no se pudo guardar cameras.json: %s", e)


load_cams()


def cam_src(c: dict) -> str:
    """URL real desde la que lee Vialert (puede llevar credenciales: no se muestra ni se registra)."""
    return f"{MTX_READ_URL}/{c['id']}" if c["type"] == "publish" else c["url"]


def _ip_parts(url: str) -> dict:
    p = urlsplit(url)
    path = p.path + (("?" + p.query) if p.query else "")
    return {"scheme": p.scheme, "host": p.hostname or "", "port": p.port, "user": unquote(p.username or ""), "path": path}


def compose_url(f: dict, old_url: str = "") -> str:
    """Arma rtsp://usuario:clave@host:puerto/ruta a partir de campos. Clave vacia = conservar la anterior."""
    scheme = (f.get("scheme") or "rtsp").lower()
    host = (f.get("host") or "").strip()
    if not host or any(ch in host for ch in " /@?#"):
        raise ValueError("host")
    port = f.get("port")
    user = (f.get("user") or "").strip()
    pw = f.get("pass") or ""
    if not pw and old_url and user and user == unquote(urlsplit(old_url).username or ""):
        pw = unquote(urlsplit(old_url).password or "")
    path = (f.get("path") or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    cred = (quote(user, safe="") + (":" + quote(pw, safe="") if pw else "") + "@") if user else ""
    hostp = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{cred}{hostp}{(':' + str(int(port))) if port else ''}{path}"


def publish_pass() -> str:
    """Clave de publicacion: .env (MTX_PUBLISH_PASS) o, si Vialert corre en la misma maquina, la de mediamtx.yml."""
    if MTX_PUBLISH_PASS:
        return MTX_PUBLISH_PASS
    try:
        txt = Path(MTX_CONFIG).read_text(encoding="utf-8")
        m = re.search(r"user:\s*" + re.escape(MTX_PUBLISH_USER) + r"\s*\n\s*pass:\s*(\S+)", txt)
        return m.group(1).strip("'\"") if m else ""
    except OSError:
        return ""


def _host_of(request: Request) -> str:
    h = PUBLIC_HOST or request.headers.get("x-forwarded-host") or request.headers.get("host") or "SERVIDOR"
    return h.split(",")[0].strip().rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"


def connect_info(c: dict, request: Request) -> dict:
    host, user, pw = _host_of(request), MTX_PUBLISH_USER, publish_pass()
    rtsp = f"rtsp://{host}:{MTX_RTSP_PORT}/{c['id']}"
    rtmp = f"rtmp://{host}:{MTX_RTMP_PORT}/{c['id']}"
    rtsp_full = f"rtsp://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{MTX_RTSP_PORT}/{c['id']}" if pw else rtsp
    rtmp_full = f"{rtmp}?user={quote(user, safe='')}&pass={quote(pw, safe='')}" if pw else rtmp
    larix = ("larix://set/v1?conn[][url]=" + quote(rtsp_full, safe="") + "&conn[][name]=" + quote(c["name"], safe="")
             + "&conn[][overwrite]=on") if pw else ""
    return {"host": host, "user": user, "password": pw, "has_password": bool(pw), "rtsp": rtsp, "rtmp": rtmp, "rtsp_full": rtsp_full,
            "rtmp_full": rtmp_full, "larix": larix, "path": c["id"], "rtsp_port": MTX_RTSP_PORT, "rtmp_port": MTX_RTMP_PORT}


def cam_state(c: dict) -> dict:
    job = JOBS.get(c.get("job_id") or "")
    running = bool(job and not job.get("finished"))
    src = job.get("live_src") if job else None
    live_info = (job["events"].last.get("live") if job and isinstance(job["events"], Hub) else None) or {}
    cnt = camlog.counts(c["id"])
    url = c["url"]
    return {"id": c["id"], "name": c["name"], "type": c["type"],
            "url": live.safe_name(url) if c["type"] == "ip" else f"/{c['id']}", "ip": _ip_parts(url) if c["type"] == "ip" else None,
            "has_auth": c["type"] == "ip" and bool(urlsplit(url).username),
            "running": running, "job_id": c.get("job_id") if running else None,
            "status": (src.status if (src and running) else "stopped"), "fps": src.fps() if (src and running) else 0,
            "size": list(src.size) if (src and src.size and running) else None, "queue": live_info.get("queue", 0),
            "elapsed": round(src.now()) if (src and running) else 0, "last": (job or {}).get("last") if job else None,
            "preview_url": f"/api/live/{c['job_id']}/mjpeg" if running else None,
            "today": cnt, "max_min": CAM_MAX_MIN}


def _lang_en(body: dict) -> bool:
    return (body or {}).get("lang") == "en"


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@app.get("/api/cams")
async def cams_list():
    return {"cams": [cam_state(c) for c in CAMS.values()], "day": camlog.today(), "max_min": CAM_MAX_MIN, "max": CAM_MAX,
            "tz": os.getenv("LOG_TZ", "America/Mexico_City"), "publish": {"user": MTX_PUBLISH_USER, "has_password": bool(publish_pass())}}


@app.post("/api/cams")
async def cam_add(request: Request):
    body = await _json(request)
    en = _lang_en(body)
    if len(CAMS) >= CAM_MAX:
        raise HTTPException(409, f"Límite de {CAM_MAX} cámaras (CAM_MAX en .env)" if not en else f"Limit of {CAM_MAX} cameras (CAM_MAX in .env)")
    typ = body.get("type")
    if typ not in ("publish", "ip"):
        raise HTTPException(400, "type debe ser publish o ip")
    url = ""
    if typ == "ip":
        try:
            url = live.check_url(body["url"] if body.get("url") else compose_url(body), en=en)
        except ValueError as e:
            msg = str(e)
            if msg == "host":
                msg = "Escribe la IP o el nombre de la cámara." if not en else "Enter the camera IP or hostname."
            raise HTTPException(400, msg) from None
    cid = f"cam{CAM_STATE['next']}"
    CAM_STATE["next"] += 1
    name = (str(body.get("name") or "").strip() or ("CAM " + cid[3:]))[:40]
    CAMS[cid] = {"id": cid, "name": name, "type": typ, "url": url, "job_id": None}
    save_cams()
    log.info("CAMARA %s agregada (%s) %s", cid, typ, live.safe_name(url) if url else "")
    out = cam_state(CAMS[cid])
    if typ == "publish":
        out["connect"] = connect_info(CAMS[cid], request)
    return out


@app.get("/api/cams/{cid}/connect")
async def cam_connect(cid: str, request: Request):
    c = CAMS.get(cid)
    if not c or c["type"] != "publish":
        raise HTTPException(404)
    return connect_info(c, request)


@app.put("/api/cams/{cid}")
async def cam_edit(cid: str, request: Request):
    c = CAMS.get(cid)
    if not c:
        raise HTTPException(404)
    body = await _json(request)
    en = _lang_en(body)
    if cam_state(c)["running"]:
        raise HTTPException(409, "Detén la cámara antes de editarla / Stop the camera before editing it")
    if "name" in body:
        c["name"] = (str(body["name"]).strip() or c["name"])[:40]
    if c["type"] == "ip" and (body.get("url") or body.get("host")):
        try:
            c["url"] = live.check_url(body["url"] if body.get("url") else compose_url(body, c["url"]), en=en)
        except ValueError as e:
            msg = str(e)
            if msg == "host":
                msg = "Escribe la IP o el nombre de la cámara." if not en else "Enter the camera IP or hostname."
            raise HTTPException(400, msg) from None
    save_cams()
    return cam_state(c)


@app.delete("/api/cams/{cid}")
async def cam_delete(cid: str, purge: int = 0):
    c = CAMS.get(cid)
    if not c:
        raise HTTPException(404)
    job = JOBS.get(c.get("job_id") or "")
    if job and not job.get("finished"):
        job["stop"].set()
    CAMS.pop(cid, None)
    CAM_STATE["gone"][cid] = c["name"]
    if purge:
        shutil.rmtree(camlog.EVENTS_DIR / cid, ignore_errors=True)
        CAM_STATE["gone"].pop(cid, None)
        camlog.forget(cid)
    save_cams()
    log.info("CAMARA %s eliminada (historial %s)", cid, "borrado" if purge else "conservado")
    return {"ok": True}


@app.post("/api/cams/probe")
async def cam_probe(request: Request):
    """Prueba de conexion: de una camara ya registrada ({id}) o de datos del formulario aun sin guardar."""
    body = await _json(request)
    en = _lang_en(body)
    waiting = False
    if body.get("id"):
        c = CAMS.get(body["id"])
        if not c:
            raise HTTPException(404)
        st = cam_state(c)
        if st["running"] and st["status"] == "live":
            return {"ok": True, "code": "ok", "size": st["size"], "message": "Analizando ahora" if not en else "Analyzing now", "running": True}
        url, waiting = cam_src(c), c["type"] == "publish"
    else:
        try:
            url = live.check_url(body["url"] if body.get("url") else compose_url(body, (CAMS.get(body.get("edit") or "") or {}).get("url", "")), en=en)
        except ValueError as e:
            msg = str(e)
            if msg == "host":
                msg = "Escribe la IP o el nombre de la cámara." if not en else "Enter the camera IP or hostname."
            raise HTTPException(400, msg) from None
    return await asyncio.to_thread(live.probe, url, en, 9.0, waiting)


@app.get("/api/qr")
async def qr_svg(data: str):
    try:
        import segno
    except ImportError:
        raise HTTPException(501, "Instala segno para generar QR (pip install segno)") from None
    if len(data) > 900:
        raise HTTPException(400)
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=6, border=2, xmldecl=False, svgns=True, nl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/api/cams/{cid}/start")
async def cam_start(cid: str, request: Request):
    c = CAMS.get(cid)
    if not c:
        raise HTTPException(404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    old = JOBS.get(c.get("job_id") or "")
    if old and not old.get("finished"):
        if old["stop"].is_set():
            raise HTTPException(409, "La cámara se está deteniendo, intenta en unos segundos / The camera is stopping, retry in a few seconds")
        return cam_state(c)
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"live": True, "cam": cid, "url": cam_src(c), "path": None, "name": c["name"], "case": None, "events": Hub(),
                 "speed": 1.0, "describe": DESCRIBE, "yolo": YOLO_ON, "hints": YOLO_HINTS, "zones": {}, "started": True,
                 "lang": "en" if (body or {}).get("lang") == "en" else "es", "max_min": CAM_MAX_MIN,
                 "created": time.time(), "report": None, "stop": asyncio.Event(), "finished": False}
    c["job_id"] = jid
    log.info("job=%s CAMARA %s iniciada %s", jid, cid, live.safe_name(cam_src(c)))
    asyncio.create_task(run_job(jid))
    return cam_state(c)


@app.post("/api/cams/{cid}/stop")
async def cam_stop(cid: str):
    c = CAMS.get(cid)
    job = JOBS.get((c or {}).get("job_id") or "")
    if not c:
        raise HTTPException(404)
    if job:
        job["stop"].set()
    return cam_state(c)


@app.get("/api/history/days")
async def history_days(cam: str):
    try:
        return {"days": camlog.days(cam)}
    except ValueError:
        raise HTTPException(400) from None


@app.get("/api/history")
async def history(cam: str, day: str | None = None):
    try:
        day = day or camlog.today()
        return {"stats": camlog.stats(cam, day), "incidents": camlog.incidents(cam, day), "days": camlog.days(cam)}
    except ValueError:
        raise HTTPException(400) from None


def _logs_args(cams, frm, to, ev, sev, down, q):
    sp = lambda v: [x for x in (v or "").split(",") if x]  # noqa: E731
    return dict(cams=sp(cams) or None, d1=frm or None, d2=to or None, keys=sp(ev) or None, sev=sp(sev) or None,
                down=down in ("1", "true", "yes"), q=q or "", names={"obs": "OBS (videos del bucket)", **CAM_STATE["gone"], **{c["id"]: c["name"] for c in CAMS.values()}})


@app.get("/api/logs")
async def logs_query(cams: str = "", frm: str = "", to: str = "", ev: str = "", sev: str = "", down: str = "", q: str = "",
                     limit: int = 500):
    try:
        r = camlog.query(limit=max(1, min(limit, 2000)), **_logs_args(cams, frm, to, ev, sev, down, q))
    except ValueError:
        raise HTTPException(400, "Parámetros inválidos / Invalid parameters") from None
    r["today"] = camlog.today()
    r["all_cams"] = [{"id": c, "name": (CAMS.get(c) or {}).get("name") or CAM_STATE["gone"].get(c) or ("OBS (videos del bucket)" if c == "obs" else c), "removed": c not in CAMS and c != "obs"} for c in sorted(set(camlog.all_cams()) | set(CAMS))]
    return r


@app.get("/api/logs/csv")
async def logs_csv(cams: str = "", frm: str = "", to: str = "", ev: str = "", sev: str = "", down: str = "", q: str = ""):
    try:
        r = camlog.query(limit=100000, **_logs_args(cams, frm, to, ev, sev, down, q))
    except ValueError:
        raise HTTPException(400) from None
    return Response(camlog.csv_rows(r["incidents"]), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="vialert_registros_{camlog.today()}.csv"'})


@app.get("/api/history/img/{cam}/{day}/{name}")
async def history_img(cam: str, day: str, name: str):
    try:
        f = camlog.day_dir(cam, day) / "img" / camlog.safe(name)
    except ValueError:
        raise HTTPException(400) from None
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/history/download")
async def history_download(cam: str, day: str, fmt: str = "csv"):
    try:
        d = camlog.day_dir(cam, day)
    except ValueError:
        raise HTTPException(400) from None
    if fmt == "csv":
        return Response(camlog.export_csv(cam, day), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="vialert_{cam}_{day}.csv"'})
    f = d / ("frames.jsonl" if fmt == "frames" else "events.jsonl")
    if not f.exists():
        raise HTTPException(404, "Sin datos")
    return FileResponse(f, media_type="application/x-ndjson", filename=f"vialert_{cam}_{day}_{fmt}.jsonl")


# ---- Escenario 2: OBS -> FunctionGraph -> /api/ingest -> cola -> YOLO -> MaaS -> resultado en OBS ----------
# Cada video que avisa FunctionGraph se registra en un libro (events/_ingest.json), entra a una cola y un trabajador
# del SERVIDOR lo baja, lo analiza y sube el informe. No hace falta navegador abierto; si el contenedor se reinicia,
# lo pendiente se retoma. Se pueden mandar muchos a la vez (o en cadena): se analizan de INGEST_WORKERS en INGEST_WORKERS.
INGEST_WORKERS = max(1, int(os.getenv("INGEST_WORKERS", "1")))          # videos de OBS que se analizan a la vez
INGEST_SPEED = max(1.0, min(float(os.getenv("INGEST_SPEED", "20")), 20.0))   # no hace falta tiempo real: ritmo de analisis del video
INGEST_RETRIES = max(0, int(os.getenv("INGEST_RETRIES", "2")))           # reintentos de la descarga
INGEST_LEDGER = camlog.EVENTS_DIR / "_ingest.json"
INGEST_ACTIVE = ("queued", "downloading", "analyzing")
INGEST_Q: asyncio.Queue | None = None
_ingest_lock = threading.Lock()


def ingest_save():
    """Guarda el libro de videos recibidos (los ultimos 500) de forma atomica."""
    with _ingest_lock:
        try:
            items = sorted(INGESTS.values(), key=lambda x: x["received"])[-500:]
            tmp = INGEST_LEDGER.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            tmp.replace(INGEST_LEDGER)
        except OSError as e:
            log.info("INGEST libro no se pudo guardar: %s", e)


def _ingest_set(iid: str, **kw):
    it = INGESTS.get(iid)
    if it is not None:
        it.update(kw)
        ingest_save()


def ingest_load() -> list[str]:
    """Lee el libro al arrancar. Devuelve los ids que hay que volver a encolar (quedaron a medias)."""
    if not INGEST_LEDGER.exists():
        return []
    try:
        items = json.loads(INGEST_LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    redo = []
    for it in sorted(items, key=lambda x: x.get("received", 0)):
        INGESTS[it["id"]] = it
        if it.get("status") in INGEST_ACTIVE:
            if it.get("attempts", 0) >= 3 or time.time() - it.get("received", 0) > 86400:
                it.update(status="error", error="se interrumpio varias veces o es muy antiguo; reenvialo")
            else:
                it.update(status="queued", stage="en cola", progress=0, attempts=it.get("attempts", 0) + 1)
                redo.append(it["id"])
        if it.get("status") != "error":
            INGEST_KEYS[f"{it['bucket']}/{it['key']}"] = it["id"]
    return redo


def _ingest_progress(iid: str, ev: dict):
    it = INGESTS.get(iid)
    if not it:
        return
    t = ev.get("type")
    if t == "scan":
        it["stage"], it["progress"] = "YOLO y fuego/humo", round(min(0.3, ev.get("pct", 0) / 100 * 0.3), 3)
    elif t == "start":
        it.update(stage="Modelo (MaaS)", n_frames=ev.get("n_frames"), done_frames=0, frames_err=0)
    elif t in ("result", "error"):
        it["done_frames"] = it.get("done_frames", 0) + 1
        if t == "error":
            it["frames_err"] = it.get("frames_err", 0) + 1
        n = it.get("n_frames") or 0
        if n:
            it["progress"] = round(0.3 + 0.7 * min(1.0, it["done_frames"] / n), 3)
    elif t == "incidents":
        it["incidents_live"] = len(ev.get("items") or [])


def ingest_publish(job: dict, log_dir: Path):
    """Sube a OBS el informe y los frames de evidencia del video analizado y cierra el registro."""
    o = job["obs"]
    iid = o["id"]
    rep = job["report"]
    incs = [{"event": i["event"], "t": i["t_start"], "t_end": i["t_end"], "conf": i.get("confidence"),
             "severity": i.get("severity"), "reason": i.get("reason")} for i in rep.get("incidents", [])]
    res = rep.get("results", [])
    n_err = sum(1 for r in res if r.get("error"))
    base_kw = dict(finished=time.time(), progress=1.0, incidents=incs, frames=len(res), frames_err=n_err,
                   analytics=(rep.get("analytics") or {}).get("counts") or {}, yolo_error=rep.get("yolo_error"),
                   duration_s=round(job.get("duration") or 0, 1))
    if job.get("fatal") or not res or n_err == len(res):
        err = job.get("fatal") or ("no se pudo leer ningun frame del video" if not res else "todos los frames fallaron en el modelo (revisa MAAS_API_KEY, cuota o red); usa Reintentar")
        _ingest_set(iid, status="error", stage="error", error=str(err)[:250], **base_kw)
        log.info("job=%s INGEST %s con error: %s", job.get("jid", "?"), iid, err)
        return
    try:
        base = f"{obsio.OUT_PREFIX}{o['key']}/"
        obsio.upload_bytes(o["bucket"], base + "report.json", json.dumps(rep, ensure_ascii=False, indent=2).encode())
        n = 1
        by_idx = {r["idx"]: r for r in res}
        for inc in rep.get("incidents", []):
            r = by_idx.get(inc.get("best_idx"))
            if r and r.get("files"):
                f = log_dir / r["files"][-1]
                if f.exists():
                    obsio.upload_file(o["bucket"], f"{base}evidence_{inc['id']:02d}_{inc['event']}.jpg", f)
                    n += 1
        _ingest_set(iid, status="done", stage="listo", result_prefix=base, uploaded=n, upload_error=None, error=None, **base_kw)
        log.info("job=%s OBS resultado subido a %s/%s (%d objetos)", job.get("jid", "?"), o["bucket"], base, n)
    except Exception as e:  # noqa: BLE001
        log.info("OBS subida fallo: %s", e)
        _ingest_set(iid, status="done", stage="listo (sin subir)", upload_error=str(e)[:200], **base_kw)


async def ingest_process(iid: str):
    it = INGESTS[iid]
    tmp = UPLOADS / f"obs_{iid}{Path(it['key']).suffix or '.mp4'}"
    t0 = time.perf_counter()
    _ingest_set(iid, status="downloading", stage="descargando", progress=0.0, started=time.time(), error=None)
    size, last = 0, None
    for n in range(INGEST_RETRIES + 1):
        try:
            size = await asyncio.to_thread(obsio.download, it["bucket"], it["key"], tmp)
            last = None
            break
        except Exception as e:  # noqa: BLE001
            last = e
            log.info("INGEST %s descarga fallo (intento %d): %s", iid, n + 1, e)
            if n < INGEST_RETRIES:
                await asyncio.sleep(5 * (n + 1))
    if last is not None:
        raise RuntimeError(f"no se pudo descargar de OBS: {last}")
    dl = round(time.perf_counter() - t0, 1)
    _ingest_set(iid, size_mb=round(size / 1e6, 1), download_s=dl, status="analyzing", stage="preparando video", progress=0.02)
    info = await new_job(tmp, Path(it["key"]).name)
    jid = info["job_id"]
    job = JOBS[jid]
    job.update(started=True, speed=INGEST_SPEED, lang="es", describe=DESCRIBE, yolo=YOLO_ON, hints=YOLO_HINTS, headless=True,
               obs={"id": iid, "bucket": it["bucket"], "key": it["key"]}, jid=jid, hist_cam="obs",
               hist_name="OBS", source=f"{it['bucket']}/{it['key']}", kind="obs",
               on_event=lambda ev, _i=iid: _ingest_progress(_i, ev))
    _ingest_set(iid, job_id=jid, video_url=info["video_url"])
    log.info("INGEST %s analizando job=%s %s/%s (%.1f MB, %.1f s de descarga)", iid, jid, it["bucket"], it["key"], size / 1e6, dl)
    t1 = time.perf_counter()
    await run_job(jid)                       # al terminar llama a ingest_publish (que cierra el registro)
    _ingest_set(iid, analysis_s=round(time.perf_counter() - t1, 1))
    job["on_event"] = None


async def ingest_worker(n: int):
    while True:
        iid = await INGEST_Q.get()
        try:
            if INGESTS.get(iid, {}).get("status") == "queued":
                await ingest_process(iid)
        except Exception as e:  # noqa: BLE001
            log.info("INGEST %s ERROR %s", iid, e)
            _ingest_set(iid, status="error", stage="error", error=str(e)[:250], finished=time.time())
            it = INGESTS.get(iid)
            if it:
                INGEST_KEYS.pop(f"{it['bucket']}/{it['key']}", None)
        finally:
            INGEST_Q.task_done()
            try:
                await asyncio.to_thread(cleanup)
            except Exception:  # noqa: BLE001
                pass


def ingest_enqueue(iid: str):
    INGEST_Q.put_nowait(iid)


@app.on_event("startup")
async def _ingest_start():
    global INGEST_Q
    INGEST_Q = asyncio.Queue()
    redo = ingest_load()
    for _ in range(INGEST_WORKERS):
        asyncio.create_task(ingest_worker(_))
    for iid in redo:
        ingest_enqueue(iid)
    if redo:
        log.info("INGEST %d video(s) pendientes se retoman", len(redo))
    log.info("INGEST cola lista · %d trabajador(es) · libro %s", INGEST_WORKERS, INGEST_LEDGER.name)


@app.post("/api/ingest", status_code=202)
async def ingest(request: Request):
    """Lo llama FunctionGraph cuando llega un objeto nuevo al bucket. Registra el video, lo encola y responde al instante."""
    if not INGEST_TOKEN:
        raise HTTPException(503, "INGEST_TOKEN no configurado en .env")
    if not secrets.compare_digest(request.headers.get("x-vialert-token", ""), INGEST_TOKEN):
        raise HTTPException(401, "token invalido")
    if not obsio.configured():
        raise HTTPException(503, "OBS no configurado (OBS_ENDPOINT, OBS_AK, OBS_SK)")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON invalido") from None
    bucket, key = str(body.get("bucket", "")).strip(), str(body.get("key", "")).strip().lstrip("/")
    if not bucket or not key or ".." in key:
        raise HTTPException(400, "faltan bucket y key")
    if not obsio.bucket_ok(bucket):
        raise HTTPException(403, "bucket no permitido (OBS_BUCKETS)")
    if not key.lower().endswith(obsio.VIDEO_EXT):
        return {"ok": True, "ignored": "no es un video"}
    if key.startswith(obsio.OUT_PREFIX):
        return {"ok": True, "ignored": "es un resultado de Vialert"}
    dup = INGEST_KEYS.get(f"{bucket}/{key}")
    d = INGESTS.get(dup) if dup else None
    if d and (d.get("status") in INGEST_ACTIVE or d.get("received", 0) > time.time() - 600):
        return {"ok": True, "id": dup, "duplicate": True}      # reintento de FunctionGraph o evento doble
    iid = uuid.uuid4().hex[:8]
    INGESTS[iid] = {"id": iid, "bucket": bucket, "key": key, "status": "queued", "stage": "en cola", "progress": 0.0,
                    "received": time.time(), "attempts": 0, "event_id": str(body.get("event_id", ""))[:80],
                    "source": str(body.get("source", "functiongraph"))[:40]}
    INGEST_KEYS[f"{bucket}/{key}"] = iid
    ingest_save()
    ahead = sum(1 for x in INGESTS.values() if x["status"] in INGEST_ACTIVE) - 1
    log.info("INGEST %s recibido %s/%s (de %s) · delante: %d", iid, bucket, key, request.client.host if request.client else "?", ahead)
    ingest_enqueue(iid)
    return {"ok": True, "id": iid, "queued": True, "ahead": max(0, ahead)}


@app.post("/api/ingest/{iid}/retry")
async def ingest_retry(iid: str):
    it = INGESTS.get(iid)
    if not it:
        raise HTTPException(404)
    if it["status"] in INGEST_ACTIVE:
        return {"ok": True, "already": True}
    it.update(status="queued", stage="en cola", progress=0.0, error=None, upload_error=None, attempts=0, received=time.time())
    INGEST_KEYS[f"{it['bucket']}/{it['key']}"] = iid
    ingest_save()
    ingest_enqueue(iid)
    return {"ok": True}


@app.get("/api/ingest/list")
async def ingest_list(limit: int = 60):
    allv = sorted(INGESTS.values(), key=lambda x: -x["received"])
    cnt = {k: sum(1 for x in allv if x["status"] == k) for k in ("queued", "downloading", "analyzing", "done", "error")}
    return {"configured": obsio.configured(), "items": allv[:max(1, min(limit, 300))], "counts": cnt, "total": len(allv),
            "workers": INGEST_WORKERS}


@app.get("/api/cases")
async def cases():
    return load_cases()


@app.post("/api/cases/{cid}/job")
async def case_job(cid: str):
    c = next((x for x in load_cases() if x["id"] == cid), None)
    if not c or not c["available"]:
        raise HTTPException(404, "Caso no disponible (falta el video en casos/)")
    return {**await new_job(CASES_DIR / c["file"], c["title"], c), "case": c}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    cleanup()
    ext = Path(file.filename or "v.mp4").suffix.lower() or ".mp4"
    if ext not in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(400, "Formato de video no soportado")
    tmp = UPLOADS / f"up_{uuid.uuid4().hex[:8]}{ext}"
    size = 0
    with open(tmp, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                f.close()
                tmp.unlink(missing_ok=True)
                raise HTTPException(413, f"El video supera {MAX_UPLOAD_MB} MB")
            f.write(chunk)
    return await new_job(tmp, file.filename or tmp.name)


@app.get("/api/video/{job_id}")
async def video(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404)
    return FileResponse(JOBS[job_id]["path"])


@app.get("/api/stream/{job_id}")
async def stream(job_id: str, speed: float = 1.0, describe: int | None = None, yolo: int | None = None,
                 hints: int | None = None, zr: str | None = None, zp: str | None = None, lang: str = "es"):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404)
    if not job["started"]:
        job["started"] = True
        job["speed"] = max(0.5, min(speed, 20))
        job["lang"] = "en" if lang == "en" else "es"
        job["describe"] = DESCRIBE if describe is None else bool(describe)
        job["yolo"] = YOLO_ON if yolo is None else bool(yolo)
        job["hints"] = YOLO_HINTS if hints is None else bool(hints)
        zones = dict(job.get("zones") or {})
        for key, raw in (("restricted", zr), ("no_parking", zp)):
            if raw is None:
                continue
            poly = parse_zone(raw)
            if poly:
                zones[key] = poly
            else:
                zones.pop(key, None)
        job["zones"] = zones
        asyncio.create_task(run_job(job_id))

    async def gen():
        evq = job["events"]
        if isinstance(evq, Hub):                   # camara: varios visores, con repeticion del estado actual
            snap, hq = evq.subscribe()
            try:
                for ev in snap:
                    yield f"data: {json.dumps(ev)}\n\n"
                    if ev["type"] == "done":
                        return
                while True:
                    try:
                        ev = await asyncio.wait_for(hq.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(ev)}\n\n"
                    if ev["type"] == "done":
                        return
            finally:
                evq.unsubscribe(hq)
        while True:
            try:
                ev = await asyncio.wait_for(evq.get(), 15)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(ev)}\n\n"
            if ev["type"] == "done":
                break

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/report/{job_id}")
async def report(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job["report"]:
        raise HTTPException(404, "Informe no disponible todavia")
    return Response(json.dumps(job["report"], ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="vialert_{job_id}.json"'})


@app.get("/api/log/{job_id}")
async def job_log(job_id: str):
    f = LOGS / job_id / "log.jsonl"
    if job_id not in JOBS or not f.exists():
        raise HTTPException(404, "Log no disponible")
    return FileResponse(f, media_type="application/x-ndjson", filename=f"vialert_{job_id}.jsonl")


@app.get("/api/frame/{job_id}/{name}")
async def job_frame(job_id: str, name: str):
    f = LOGS / job_id / name
    if job_id not in JOBS or not re.fullmatch(r"\d{3}_\d\.jpg", name) or not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")


EXPLAIN_LOCK = asyncio.Semaphore(1)


@app.post("/api/explain/{job_id}/{idx}")
async def explain(job_id: str, idx: int):
    """Diagnostico bajo demanda: reenvia los mismos frames con el razonamiento (thinking) activado."""
    if job_id not in JOBS:
        raise HTTPException(404)
    i18n.LANG.set(JOBS[job_id].get("lang", "es"))
    files = sorted((LOGS / job_id).glob(f"{idx:03d}_*.jpg"))
    if not files:
        raise HTTPException(404, "No hay frames guardados para ese punto")
    if MOCK:
        return {"mock": True, "reasoning": i18n.pick("(modo simulado: aqui aparece el razonamiento real del modelo)", "(simulated mode: the model's real reasoning appears here)"),
                "content": "{}", "latency_ms": 0, "usage": {}}
    frames = [base64.b64encode(f.read_bytes()).decode() for f in files]
    content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in frames]
    content.append({"type": "text", "text": "Clasifica la ultima imagen."})
    body = {"model": MAAS_MODEL, "stream": False, "thinking": {"type": "enabled"}, "max_completion_tokens": 3000,
            "messages": [{"role": "system", "content": system_prompt(True)}, {"role": "user", "content": content}]}
    headers = {"Authorization": f"Bearer {MAAS_API_KEY}", "Content-Type": "application/json"}
    async with EXPLAIN_LOCK:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post(MAAS_URL, json=body, headers=headers, timeout=120)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Error de red hacia MaaS: {type(e).__name__}") from None
        ms = round((time.perf_counter() - t0) * 1000)
    if r.status_code != 200:
        raise HTTPException(502, str(MaasError(r.status_code, r.text)))
    d = r.json()
    msg = d["choices"][0]["message"]
    out = {"reasoning": msg.get("reasoning_content") or "", "content": msg.get("content") or "",
           "latency_ms": ms, "usage": d.get("usage") or {}}
    log.info("job=%s EXPLICAR #%03d %dms usage=%s", job_id, idx, ms, out["usage"])
    with open(LOGS / job_id / "explain.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"idx": idx, **out}, ensure_ascii=False) + "\n")
    return out


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
