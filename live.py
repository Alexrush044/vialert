"""Entrada en vivo (RTSP / RTMP): un hilo lee el stream sin parar y deja siempre disponible el ultimo frame.

No depende de app.py. La logica de analisis (YOLO, calidad, envio a MaaS) vive en app.live_pipeline.
Con camaras reales la URL es rtsp://usuario:clave@IP:554/ruta; en el demo con celular, el celular publica en
MediaMTX (en la propia ECS) y Vialert lee rtsp://127.0.0.1:8554/cam1.
"""
import collections
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import cv2

# TCP evita perdida de paquetes por UDP; nobuffer/low_delay reducen el retraso de lectura.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                      "rtsp_transport;" + os.getenv("LIVE_TRANSPORT", "tcp") + "|fflags;nobuffer|flags;low_delay")

ALLOWED_SCHEMES = ("rtsp", "rtsps", "rtmp", "rtmps")
OPEN_TIMEOUT_MS = int(os.getenv("LIVE_OPEN_TIMEOUT_MS", "8000"))
READ_TIMEOUT_MS = int(os.getenv("LIVE_READ_TIMEOUT_MS", "8000"))


def check_url(url: str, en: bool = False) -> str:
    """Valida el esquema (evita file://, http a servicios internos, etc.) y devuelve la URL limpia."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        raise ValueError("The URL must be rtsp:// or rtmp:// with a host (e.g. rtsp://127.0.0.1:8554/cam1)" if en else
                         "La URL debe ser rtsp:// o rtmp:// con host (por ejemplo rtsp://127.0.0.1:8554/cam1)")
    allowed = [h.strip() for h in os.getenv("LIVE_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed and parts.hostname not in allowed:
        raise ValueError("Host not allowed (LIVE_ALLOWED_HOSTS)" if en else "Host no permitido (LIVE_ALLOWED_HOSTS)")
    return url


def safe_name(url: str) -> str:
    """URL sin usuario ni clave, para mostrar en pantalla y en logs."""
    p = urlsplit(url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return urlunsplit((p.scheme, host, p.path, "", ""))


class LiveSource:
    def __init__(self, url: str, ring_s: float = 8.0, ring_step: float = 0.25, ring_width: int = 960):
        self.url = url
        self.ring_step, self.ring_width = ring_step, ring_width
        self.ring: collections.deque = collections.deque(maxlen=max(4, int(ring_s / ring_step)))
        self.t0 = time.monotonic()
        self.status = "connecting"          # connecting | live | reconnecting | stopped
        self.reconnects = 0
        self.frames = 0
        self.size: tuple[int, int] | None = None
        self.error = ""
        self._latest: tuple[float, object] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fps_hist: collections.deque = collections.deque(maxlen=60)
        self._thread = threading.Thread(target=self._run, name="live-reader", daemon=True)

    # ---- API para el resto de la app ----------------------------------------------
    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.status = "stopped"

    def now(self) -> float:
        return time.monotonic() - self.t0

    def latest(self):
        """(t, frame BGR) del ultimo frame leido, o None."""
        with self._lock:
            return self._latest

    def ring_at(self, t: float):
        """Frame (reducido) mas cercano a t que no sea posterior; None si no hay historia tan atras."""
        with self._lock:
            best = None
            for tt, im in self.ring:
                if tt <= t + 1e-6:
                    best = (tt, im)
                else:
                    break
            return best

    def fps(self) -> float:
        h = list(self._fps_hist)
        return round((len(h) - 1) / (h[-1] - h[0]), 1) if len(h) > 2 and h[-1] > h[0] else 0.0

    # ---- hilo lector --------------------------------------------------------------
    def _open(self):
        try:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG,
                                   [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS])
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            return None
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _run(self):
        backoff = 1.0
        last_ring = -1e9
        while not self._stop.is_set():
            cap = self._open()
            if cap is None:
                self.status = "connecting" if self.frames == 0 else "reconnecting"
                self.error = self.error or "no se pudo abrir el stream"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            backoff = 1.0
            self.status = "live"
            self.error = ""
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok or img is None:
                    break
                t = self.now()
                self.frames += 1
                self._fps_hist.append(t)
                if self.size is None:
                    self.size = (img.shape[1], img.shape[0])
                small = None
                if t - last_ring >= self.ring_step:
                    last_ring = t
                    h, w = img.shape[:2]
                    small = cv2.resize(img, (self.ring_width, int(h * self.ring_width / w)),
                                       interpolation=cv2.INTER_AREA) if w > self.ring_width else img.copy()
                with self._lock:
                    self._latest = (t, img)
                    if small is not None:
                        self.ring.append((t, small))
            cap.release()
            if not self._stop.is_set():
                self.status = "reconnecting"
                self.reconnects += 1
                self._stop.wait(1.0)
        self.status = "stopped"


# ---- Prueba de conexion (asistente "Agregar camara") --------------------------------------------------
import json as _json
import re as _re
import shutil as _shutil
import subprocess as _sp

_ERR = [  # (patron en la salida de ffprobe, codigo)
    (r"401|unauthorized|authorization failed|authentication", "auth"),
    (r"403|forbidden", "forbidden"),
    (r"404|not found", "path"),
    (r"connection refused", "refused"),
    (r"timed out|timeout|no route to host|network is unreachable|host is unreachable", "unreachable"),
    (r"name or service not known|could not resolve|nodename nor servname|no address associated", "dns"),
    (r"invalid data found|no video|does not contain|could not find codec", "novideo"),
]
_MSG = {
    "auth": ("Usuario o contraseña incorrectos (la cámara respondió 401).", "Wrong username or password (the camera answered 401)."),
    "forbidden": ("La cámara rechazó el acceso (403): revisa permisos del usuario o RTSP habilitado.", "The camera refused access (403): check user permissions or that RTSP is enabled."),
    "path": ("La cámara responde pero la ruta del video no existe (404). Revisa la ruta del stream.", "The camera answers but the stream path does not exist (404). Check the stream path."),
    "refused": ("Puerto cerrado o RTSP desactivado en la cámara (conexión rechazada). Revisa el puerto.", "Port closed or RTSP disabled on the camera (connection refused). Check the port."),
    "unreachable": ("No hay ruta hacia la cámara (tiempo agotado). Revisa IP, red/VPN, firewall o que la cámara esté encendida.", "No route to the camera (timed out). Check IP, network/VPN, firewall, or that the camera is powered on."),
    "dns": ("No se encontró ese nombre de host. Revisa la dirección.", "That hostname could not be resolved. Check the address."),
    "novideo": ("Conectó, pero no se detectó video H.264/H.265 en ese stream.", "Connected, but no H.264/H.265 video was found on that stream."),
    "waiting": ("Todavía no llega señal. Inicia la transmisión desde el celular o equipo.", "No signal yet. Start streaming from the phone or device."),
    "server": ("No responde el servidor de video (MediaMTX). Verifica que esté corriendo y que MTX_READ_URL sea correcto.", "The video server (MediaMTX) is not answering. Check that it is running and that MTX_READ_URL is correct."),
    "server_auth": ("Vialert no tiene permiso para leer del servidor de video: revisa MTX_READ_URL (usuario/clave de lectura).", "Vialert is not allowed to read from the video server: check MTX_READ_URL (read user/password)."),
    "timeout": ("La prueba tardó demasiado: el equipo no respondió a tiempo.", "The test took too long: the device did not answer in time."),
    "error": ("No se pudo abrir el stream.", "Could not open the stream."),
}


def _scrub(text: str, url: str) -> str:
    p = urlsplit(url)
    for secret in (p.password, p.username):
        if secret:
            text = text.replace(secret, "***")
    return text.replace(url, safe_name(url))[:300]


def probe(url: str, en: bool = False, timeout_s: float = 9.0, waiting_ok: bool = False) -> dict:
    """Abre el stream unos segundos y dice si hay video. Usa ffprobe (mensajes claros); si no existe, OpenCV.
    waiting_ok=True: un 404 significa 'la ruta aun no recibe señal' (caso del celular que va a publicar)."""
    i = 1 if en else 0
    res = {"ok": False, "code": "error", "message": "", "size": None, "codec": None, "detail": ""}
    ff = _shutil.which("ffprobe")
    if ff:
        cmd = [ff, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name", "-of", "json"]
        if urlsplit(url).scheme.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", str(int(timeout_s * 1e6))]
        else:
            cmd += ["-rw_timeout", str(int(timeout_s * 1e6))]
        try:
            r = _sp.run(cmd + [url], capture_output=True, text=True, timeout=timeout_s + 4)
            out, err = r.stdout, r.stderr
        except _sp.TimeoutExpired:
            res.update(code="timeout", message=_MSG["timeout"][i])
            return res
        try:
            st = (_json.loads(out or "{}").get("streams") or [None])[0]
        except ValueError:
            st = None
        if st and st.get("width"):
            res.update(ok=True, code="ok", size=[st["width"], st["height"]], codec=st.get("codec_name"),
                       message="Video recibido" if not en else "Video received")
            return res
        low = (err or "").lower()
        code = next((c for pat, c in _ERR if _re.search(pat, low)), "novideo" if r.returncode == 0 else "error")
        if waiting_ok:
            code = {"path": "waiting", "refused": "server", "unreachable": "server", "dns": "server", "auth": "server_auth", "forbidden": "server_auth"}.get(code, code)
        res.update(code=code, message=_MSG[code][i], detail=_scrub((err or "").strip().splitlines()[-1] if (err or "").strip() else "", url))
        return res
    cap = None
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(timeout_s * 1000), cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(timeout_s * 1000)])
        ok, img = (cap.read() if cap.isOpened() else (False, None))
        if ok and img is not None:
            res.update(ok=True, code="ok", size=[img.shape[1], img.shape[0]], message="Video recibido" if not en else "Video received")
        else:
            res.update(code="waiting" if waiting_ok else "unreachable", message=_MSG["waiting" if waiting_ok else "unreachable"][i])
    except Exception as e:  # noqa: BLE001
        res.update(code="error", message=_MSG["error"][i], detail=_scrub(f"{type(e).__name__}", url))
    finally:
        if cap is not None:
            cap.release()
    return res
