"""Etapa YOLO (Ultralytics): deteccion de objetos + seguimiento simple + analitica + disparadores para el VLM.

YOLO NO decide el incidente: detecta autos/personas, sigue a cada uno entre muestras y levanta
"disparadores" (vehiculo que se detiene, detencion brusca cerca de otro vehiculo, persona en
posicion horizontal). Los disparadores hacen que se envie un frame extra al VLM, y un resumen
de lo detectado puede acompanar el frame como pista. La clasificacion final la hace el VLM.
"""
import os
from pathlib import Path

import i18n
import threading

# Perfil "detallado" por defecto: modelo s (mas preciso que n), imagen mas grande y umbrales mas sensibles.
# Si la ECS va justa de CPU (muchas camaras): YOLO_WEIGHTS=yolo11n.pt YOLO_IMGSZ=640 YOLO_FPS=2 en el .env.
YOLO_WEIGHTS = os.getenv("YOLO_WEIGHTS", "yolo11s.pt")
YOLO_IMGSZ = int(os.getenv("YOLO_IMGSZ", "800"))
YOLO_CONF = float(os.getenv("YOLO_CONF", "0.25"))
YOLO_FPS = float(os.getenv("YOLO_FPS", "3"))            # muestras por segundo de video analizadas con YOLO
YOLO_LIVE_FPS = float(os.getenv("YOLO_LIVE_FPS", "2"))  # idem en camaras en vivo (varias camaras comparten CPU)
YOLO_BATCH = int(os.getenv("YOLO_BATCH", "8"))
STOP_S = float(os.getenv("YOLO_STOP_S", "3"))           # s quieto (tras haberse movido) para 'detenido'
MOVE_W_PER_S = float(os.getenv("YOLO_MOVE_THR", "0.5"))  # anchos de vehiculo por segundo que cuentan como 'en movimiento'
LIE_RATIO = float(os.getenv("YOLO_LIE_RATIO", "1.1"))
CROWD_N = int(os.getenv("YOLO_CROWD_N", "5"))            # personas simultaneas para 'aglomeracion'
CONTACT_IOU = float(os.getenv("YOLO_CONTACT_IOU", "0.10"))  # solape entre dos personas para 'contacto'
VEH_CONTACT_IOU = float(os.getenv("YOLO_VEH_CONTACT_IOU", "0.08"))  # solape entre dos vehiculos (con frenado) para 'contacto'
LOITER_S = float(os.getenv("YOLO_LOITER_S", "15"))       # s en el mismo lugar para 'merodeo'
LOITER_MOVE_W = float(os.getenv("YOLO_LOITER_MOVE", "1.5"))  # desplazamiento maximo (anchos de persona) para 'merodeo'
ABANDON_S = float(os.getenv("YOLO_ABANDON_S", "15"))     # s sin persona cerca para 'objeto abandonado'
PARK_S = float(os.getenv("YOLO_PARK_S", "8"))            # s detenido en zona de no estacionar
OBJ_CONF = float(os.getenv("YOLO_OBJ_CONF", "0.30"))     # confianza minima para mochilas/bolsos/maletas
FLOW_BIN_S = float(os.getenv("YOLO_FLOW_BIN", "5"))
HIST_S = max(STOP_S, PARK_S, 3.0) + 3
   # ancho/alto de una persona para considerarla horizontal

COCO = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck",
        24: "backpack", 26: "handbag", 28: "suitcase",
        16: "dog", 17: "horse", 19: "cow", 43: "knife", 34: "baseball bat"}
# OJO: el orden de CLASSES es el indice que usa el front para dibujar cajas; las clases nuevas van al final.
CLASSES = ["person", "bicycle", "car", "motorcycle", "bus", "truck", "backpack", "handbag", "suitcase",
           "dog", "horse", "cow", "knife", "baseball bat"]
ES = {"person": ("persona", "personas"), "bicycle": ("bicicleta", "bicicletas"), "car": ("auto", "autos"),
      "motorcycle": ("moto", "motos"), "bus": ("autobus", "autobuses"), "truck": ("camion", "camiones"),
      "backpack": ("mochila", "mochilas"), "handbag": ("bolso", "bolsos"), "suitcase": ("maleta", "maletas"),
      "dog": ("perro", "perros"), "horse": ("caballo", "caballos"), "cow": ("vaca", "vacas"),
      "knife": ("cuchillo", "cuchillos"), "baseball bat": ("bate", "bates")}
EN_NAMES = {"person": ("person", "people"), "bicycle": ("bicycle", "bicycles"), "car": ("car", "cars"),
            "motorcycle": ("motorbike", "motorbikes"), "bus": ("bus", "buses"), "truck": ("truck", "trucks"),
            "backpack": ("backpack", "backpacks"), "handbag": ("handbag", "handbags"), "suitcase": ("suitcase", "suitcases"),
            "dog": ("dog", "dogs"), "horse": ("horse", "horses"), "cow": ("cow", "cows"),
            "knife": ("knife", "knives"), "baseball bat": ("bat", "bats")}
WHY_EN = {"yolo:detenido": "vehicle stopped after moving", "yolo:frenado": "sudden stop of a vehicle",
          "yolo:frenado_cerca": "sudden stop next to another vehicle", "yolo:persona_horizontal": "person in a horizontal position",
          "yolo:aglomeracion": "crowd of people",
          "yolo:merodeo": "person standing in the same place for a long time (possible loitering)",
          "yolo:objeto_abandonado": "object (backpack, bag or suitcase) with no person nearby for a long time",
          "yolo:intrusion": "entry into a restricted zone", "yolo:estacionado_prohibido": "vehicle stopped in a no-parking zone",
          "yolo:forcejeo": "people in contact and moving (possible fight or struggle)",
          "yolo:contacto_vehiculos": "two vehicles overlapping while one brakes hard (possible collision)",
          "yolo:persona_vehiculo": "person overlapping a vehicle (possible pedestrian strike)",
          "yolo:animal": "animal on the scene", "yolo:objeto_peligroso": "knife or bat next to a person"}
VEHICLES = {"car", "motorcycle", "bus", "truck"}
OBJECTS = {"backpack", "handbag", "suitcase"}
ANIMALS = {"dog", "horse", "cow"}
WEAPONS = {"knife", "baseball bat"}


def _cat(c: str) -> str:
    return ("veh" if c in VEHICLES else "obj" if c in OBJECTS else "ani" if c in ANIMALS
            else "wpn" if c in WEAPONS else "per")
WHY_ES = {"yolo:detenido": "vehiculo detenido tras circular", "yolo:frenado": "detencion brusca de un vehiculo",
          "yolo:frenado_cerca": "detencion brusca junto a otro vehiculo",
          "yolo:persona_horizontal": "persona en posicion horizontal",
          "yolo:aglomeracion": "aglomeracion de personas",
          "yolo:merodeo": "persona quieta en el mismo lugar por mucho tiempo (posible merodeo)",
          "yolo:objeto_abandonado": "objeto (mochila, bolso o maleta) sin persona cerca por mucho tiempo",
          "yolo:intrusion": "ingreso a zona restringida",
          "yolo:estacionado_prohibido": "vehiculo detenido en zona de no estacionar",
          "yolo:forcejeo": "personas en contacto y en movimiento (posible pelea o forcejeo)",
          "yolo:contacto_vehiculos": "dos vehiculos encimados y uno frena de golpe (posible choque)",
          "yolo:persona_vehiculo": "persona encimada con un vehiculo (posible atropello)",
          "yolo:animal": "animal en la escena", "yolo:objeto_peligroso": "cuchillo o bate junto a una persona"}

_model = None
_lock = threading.Lock()
_infer_lock = threading.Lock()   # una inferencia a la vez: varias camaras comparten los mismos 4 vCPU
_load_error: str | None = None


def load_error() -> str | None:
    return _load_error


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("ultralytics") is not None


def resolve_weights() -> str:
    """Ruta de los pesos: el archivo tal cual, o models/<nombre> (viene en el repo), o el nombre (Ultralytics lo baja)."""
    w = YOLO_WEIGHTS
    if os.path.isabs(w) or Path(w).exists():
        return w
    p = Path(__file__).parent / "models" / w
    return str(p) if p.exists() else w


def get_model():
    global _model, _load_error
    with _lock:
        if _model is None:
            try:
                from ultralytics import YOLO
                _model = YOLO(resolve_weights())
                _load_error = None
            except Exception as e:  # noqa: BLE001
                _load_error = f"{type(e).__name__}: {str(e)[:200]}"
                raise
        return _model


def warmup() -> dict:
    """Carga el modelo y hace una inferencia de prueba. -> {'ok', 'weights', 'ms', 'error'}. Se llama al arrancar."""
    import time
    import numpy as np
    out = {"ok": False, "weights": resolve_weights(), "ms": None, "error": None}
    try:
        t0 = time.perf_counter()
        detect([np.zeros((360, 640, 3), dtype=np.uint8)])
        out.update(ok=True, ms=round((time.perf_counter() - t0) * 1000))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def detect(frames_bgr: list) -> list[list[dict]]:
    """Lista de imagenes BGR -> por imagen, lista de detecciones {c, x1, y1, x2, y2, conf} normalizadas (0-1)."""
    if not frames_bgr:
        return []
    model = get_model()
    with _infer_lock:
        results = model.predict(source=frames_bgr, imgsz=YOLO_IMGSZ, conf=YOLO_CONF, classes=list(COCO),
                                device="cpu", verbose=False)
    out = []
    for r in results:
        dets = []
        b = r.boxes
        if b is not None and len(b):
            xy = b.xyxyn.cpu().numpy()
            cl = b.cls.cpu().numpy().astype(int)
            cf = b.conf.cpu().numpy()
            for (x1, y1, x2, y2), c, p in zip(xy, cl, cf):
                if int(c) in COCO and (COCO[int(c)] not in (OBJECTS | WEAPONS) or float(p) >= OBJ_CONF):
                    dets.append({"c": COCO[int(c)], "x1": float(x1), "y1": float(y1), "x2": float(x2),
                                 "y2": float(y2), "conf": float(p)})
        out.append(dets)
    return out


# ---- seguimiento simple + reglas (sin dependencias de tracking) -------------------------------
def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


class Track:
    def __init__(self, tid: int, cls: str, box, t: float):
        self.id, self.cls, self.box, self.t_first, self.t_last = tid, cls, box, t, t
        self.hist = [(t, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0], box[3] - box[1])]
        self.flag = ""            # motivo por el que se marco (para dibujar la caja en rojo)
        self.fired: set[str] = set()
        self.lying_count = 0
        self.c0 = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)   # centro inicial (merodeo / objeto abandonado)
        self.n = 1
        self.last_near_t = t      # ultima vez con una persona cerca (objetos)
        self.in_restr = 0

    def add(self, box, t):
        self.box, self.t_last = box, t
        self.hist.append((t, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0], box[3] - box[1]))
        self.n += 1
        self.hist = [h for h in self.hist if t - h[0] <= HIST_S]

    def from_start_w(self) -> float:
        """Desplazamiento total desde donde aparecio, en anchos de caja."""
        w = max(1e-6, sum(h[3] for h in self.hist) / len(self.hist))
        cx, cy = (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2
        return ((cx - self.c0[0]) ** 2 + (cy - self.c0[1]) ** 2) ** 0.5 / w

    def speed_w(self) -> float | None:
        """Velocidad instantanea (anchos de caja por segundo) entre las dos ultimas muestras."""
        if len(self.hist) < 2:
            return None
        a, b = self.hist[-2], self.hist[-1]
        dt = b[0] - a[0]
        if dt <= 0 or dt > 1.2:
            return None
        w = max(1e-6, (a[3] + b[3]) / 2)
        return ((b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2) ** 0.5 / w / dt

    def disp_w(self, t0: float, t1: float) -> float | None:
        """Desplazamiento del centro entre t0 y t1, en anchos de caja (None si faltan datos)."""
        pts = [h for h in self.hist if t0 <= h[0] <= t1]
        if len(pts) < 2:
            return None
        a, b = pts[0], pts[-1]
        w = max(1e-6, sum(p[3] for p in pts) / len(pts))
        return ((b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2) ** 0.5 / w


def _inside(pt, poly) -> bool:
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1:
            inside = not inside
    return inside


def _flow(stats: list, tracks: list, duration: float) -> dict:
    """Conteo y flujo (aproximado) a partir de las muestras de YOLO."""
    import statistics
    bins: dict[int, list] = {}
    for st in stats:
        bins.setdefault(int(st["t"] // FLOW_BIN_S), []).append(st)
    out_bins = []
    for k in sorted(bins):
        b = bins[k]
        sp = [v for s_ in b for v in s_["speeds"]]
        out_bins.append({"t": round(k * FLOW_BIN_S, 1), "veh": round(sum(s_["veh"] for s_ in b) / len(b), 1),
                         "per": round(sum(s_["per"] for s_ in b) / len(b), 1),
                         "occ": round(sum(s_["occ"] for s_ in b) / len(b), 3),
                         "speed": round(statistics.median(sp), 2) if sp else None})
    good = [tr for tr in tracks if tr.n >= 3]
    uveh = sum(1 for tr in good if tr.cls in VEHICLES)
    uper = sum(1 for tr in good if tr.cls == "person")
    mins = max(duration, 1e-6) / 60
    occ_mean = sum(b["occ"] for b in out_bins) / len(out_bins) if out_bins else 0.0
    sps = [b["speed"] for b in out_bins if b["speed"] is not None]
    sp_mean = sum(sps) / len(sps) if sps else None
    third = max(1, len(out_bins) // 3)
    trend = "sin datos"
    if len(out_bins) >= 3:
        a = sum(b["occ"] for b in out_bins[:third]) / third
        z = sum(b["occ"] for b in out_bins[-third:]) / third
        trend = "aumenta" if z > a * 1.15 + 0.01 else ("disminuye" if z < a * 0.85 - 0.01 else "estable")
    if occ_mean >= 0.18 and sp_mean is not None and sp_mean < 0.4:
        state = "congestionado"
    elif occ_mean >= 0.10:
        state = "denso"
    else:
        state = "fluido"
    peak = max(out_bins, key=lambda b: b["veh"]) if out_bins else None
    return {"bins": out_bins, "bin_s": FLOW_BIN_S, "unique_vehicles": uveh, "unique_persons": uper,
            "vehicles_per_min": round(uveh / mins, 1), "persons_per_min": round(uper / mins, 1),
            "occupancy_mean": round(occ_mean, 3), "speed_mean": round(sp_mean, 2) if sp_mean is not None else None,
            "trend": trend, "state": state, "peak_vehicles": peak["veh"] if peak else 0, "peak_t": peak["t"] if peak else None}


def analyze(samples: list[tuple[float, list[dict]]], zones: dict | None = None) -> dict:
    """samples: [(t, dets)] en orden. zones: {'restricted': [[x,y],...], 'no_parking': [[x,y],...]} normalizadas.
    Devuelve {'boxes': [[t, [[ci,x1,y1,x2,y2,tid,flag],...]]], 'triggers': [...], 'flow': {...}}."""
    zones = zones or {}
    tracks: list[Track] = []
    next_id = 1
    triggers: list[dict] = []
    boxes_out = []

    def fire(tr: Track, t: float, why: str, detail: str = ""):
        if why in tr.fired:
            return
        tr.fired.add(why)
        tr.flag = why
        triggers.append({"t": round(t, 2), "why": why, "label": _why(why), "track": tr.id, "cls": tr.cls,
                         "detail": detail})

    stats: list[dict] = []
    crowd_run = 0
    crowd_last = -1e9
    contact: dict[tuple[int, int], int] = {}
    fired_pairs: set[tuple[int, int]] = set()
    vcontact: dict[tuple[int, int], int] = {}
    fired_vpairs: set[tuple[int, int]] = set()
    pv: dict[tuple[int, int], int] = {}

    for t, dets in samples:
        # 1) asociar detecciones con tracks (IoU, y distancia de centros como respaldo)
        live = [tr for tr in tracks if t - tr.t_last <= 1.6]
        used: set[int] = set()
        assigned: list[tuple[dict, Track | None]] = []
        pairs = []
        for i, d in enumerate(dets):
            db = (d["x1"], d["y1"], d["x2"], d["y2"])
            for tr in live:
                if _cat(d["c"]) != _cat(tr.cls) or (_cat(d["c"]) in ("obj", "ani", "wpn") and d["c"] != tr.cls):
                    continue
                iou = _iou(db, tr.box)
                cx, cy = (db[0] + db[2]) / 2, (db[1] + db[3]) / 2
                tx, ty = (tr.box[0] + tr.box[2]) / 2, (tr.box[1] + tr.box[3]) / 2
                dist = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
                w = max(db[2] - db[0], tr.box[2] - tr.box[0], 1e-6)
                score = iou if iou >= 0.2 else (0.15 - 0.1 * dist / w if dist <= 1.2 * w else 0)
                if score > 0:
                    pairs.append((score, i, tr))
        pairs.sort(key=lambda p: -p[0])
        matched_det: dict[int, Track] = {}
        for score, i, tr in pairs:
            if i in matched_det or id(tr) in used:
                continue
            matched_det[i] = tr
            used.add(id(tr))
        for i, d in enumerate(dets):
            db = (d["x1"], d["y1"], d["x2"], d["y2"])
            tr = matched_det.get(i)
            if tr is None:
                tr = Track(next_id, d["c"], db, t)
                next_id += 1
                tracks.append(tr)
            else:
                tr.add(db, t)
            assigned.append((d, tr))

        # 2) reglas
        for d, tr in assigned:
            if tr.cls in VEHICLES:
                still = tr.disp_w(t - STOP_S, t)
                before = tr.disp_w(t - STOP_S - 3, t - STOP_S + 0.01)
                if (still is not None and tr.t_last - tr.t_first >= STOP_S and still <= 0.25
                        and before is not None and before >= MOVE_W_PER_S * 2):
                    fire(tr, t, "yolo:detenido")
                v_now = tr.disp_w(t - 1.0, t)
                v_prev = tr.disp_w(t - 2.5, t - 1.0)
                if v_now is not None and v_prev is not None and v_prev >= MOVE_W_PER_S * 1.2 and v_now <= 0.15:
                    near = False
                    for _, other in assigned:
                        if other is tr or other.cls not in VEHICLES:
                            continue
                        dx = ((tr.box[0] + tr.box[2]) - (other.box[0] + other.box[2])) / 2
                        dy = ((tr.box[1] + tr.box[3]) - (other.box[1] + other.box[3])) / 2
                        reach = 1.2 * ((tr.box[2] - tr.box[0]) + (other.box[2] - other.box[0])) / 2
                        if (dx * dx + dy * dy) ** 0.5 <= reach or _iou(tr.box, other.box) > 0:
                            near = True
                            break
                    fire(tr, t, "yolo:frenado_cerca" if near else "yolo:frenado")
            elif tr.cls == "person":
                w, h = d["x2"] - d["x1"], d["y2"] - d["y1"]
                if h > 0.03 and w / max(h, 1e-6) >= LIE_RATIO:
                    tr.lying_count += 1
                    if tr.lying_count >= 2:
                        fire(tr, t, "yolo:persona_horizontal")
                else:
                    tr.lying_count = 0

        # 3) reglas de personas en grupo: aglomeracion y contacto con movimiento (posible pelea/forcejeo)
        people = [(d, tr) for d, tr in assigned if tr.cls == "person"]
        crowd_run = crowd_run + 1 if len(people) >= CROWD_N else 0
        if crowd_run >= 2 and t - crowd_last >= 10:
            crowd_last = t
            tr0 = people[0][1]
            triggers.append({"t": round(t, 2), "why": "yolo:aglomeracion", "label": WHY_ES["yolo:aglomeracion"],
                             "track": tr0.id, "cls": "person"})
            for _, tr in people:
                tr.flag = tr.flag or "yolo:aglomeracion"
        seen_pairs = set()
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                (da, ta), (db_, tb) = people[i], people[j]
                if _iou((da["x1"], da["y1"], da["x2"], da["y2"]), (db_["x1"], db_["y1"], db_["x2"], db_["y2"])) < CONTACT_IOU:
                    continue
                key = (min(ta.id, tb.id), max(ta.id, tb.id))
                seen_pairs.add(key)
                contact[key] = contact.get(key, 0) + 1
                moving = max(ta.disp_w(t - 1.5, t) or 0, tb.disp_w(t - 1.5, t) or 0)
                if contact[key] >= 2 and moving >= 0.35 and key not in fired_pairs:
                    fired_pairs.add(key)
                    ta.flag = tb.flag = "yolo:forcejeo"
                    triggers.append({"t": round(t, 2), "why": "yolo:forcejeo", "label": WHY_ES["yolo:forcejeo"],
                                     "track": ta.id, "cls": "person"})
        for key in list(contact):
            if key not in seen_pairs:
                contact[key] = 0

        # 3b) vehiculos encimados con frenado brusco (posible choque), persona encimada con vehiculo (posible
        #     atropello), animales y armas blancas junto a personas. Solo piden un frame extra: decide el VLM.
        vehs = [(d, tr) for d, tr in assigned if tr.cls in VEHICLES]
        seen_v = set()
        for i in range(len(vehs)):
            for j in range(i + 1, len(vehs)):
                (da, ta), (db_, tb) = vehs[i], vehs[j]
                if _iou((da["x1"], da["y1"], da["x2"], da["y2"]), (db_["x1"], db_["y1"], db_["x2"], db_["y2"])) < VEH_CONTACT_IOU:
                    continue
                key = (min(ta.id, tb.id), max(ta.id, tb.id))
                seen_v.add(key)
                vcontact[key] = vcontact.get(key, 0) + 1
                braked = False
                for x in (ta, tb):
                    vn, vp = x.disp_w(t - 1.0, t), x.disp_w(t - 2.5, t - 1.0)
                    if vn is not None and vp is not None and vp >= MOVE_W_PER_S * 1.2 and vn <= 0.25:
                        braked = True
                if vcontact[key] >= 2 and braked and key not in fired_vpairs:
                    fired_vpairs.add(key)
                    ta.flag = tb.flag = "yolo:contacto_vehiculos"
                    triggers.append({"t": round(t, 2), "why": "yolo:contacto_vehiculos", "label": _why("yolo:contacto_vehiculos"),
                                     "track": ta.id, "cls": ta.cls})
        for key in list(vcontact):
            if key not in seen_v:
                vcontact[key] = 0
        for d, tr in assigned:
            if tr.cls == "person":
                pa = max(1e-9, (tr.box[2] - tr.box[0]) * (tr.box[3] - tr.box[1]))
                for dv, tv in vehs:
                    if tv.cls == "motorcycle":
                        continue                     # el motociclista siempre se encima con su moto
                    vb = (dv["x1"], dv["y1"], dv["x2"], dv["y2"])
                    ix = max(0.0, min(tr.box[2], vb[2]) - max(tr.box[0], vb[0]))
                    iy = max(0.0, min(tr.box[3], vb[3]) - max(tr.box[1], vb[1]))
                    if ix * iy / pa < 0.2:
                        continue
                    cx, cy = (tr.box[0] + tr.box[2]) / 2, (tr.box[1] + tr.box[3]) / 2
                    vw, vh = vb[2] - vb[0], vb[3] - vb[1]
                    if vb[0] + 0.2 * vw <= cx <= vb[2] - 0.2 * vw and vb[1] + 0.2 * vh <= cy <= vb[3] - 0.2 * vh:
                        continue                     # persona "dentro" del auto (conductor/pasajero visto por el parabrisas)
                    pv[(tr.id, tv.id)] = pv.get((tr.id, tv.id), 0) + 1
                    if pv[(tr.id, tv.id)] >= 2:
                        fire(tr, t, "yolo:persona_vehiculo")
            elif tr.cls in ANIMALS:
                if tr.n >= 3:
                    fire(tr, t, "yolo:animal", ES[tr.cls][0])
            elif tr.cls in WEAPONS:
                if tr.n >= 2:
                    for dp, tp in people:
                        pb = tp.box
                        pw, ph = pb[2] - pb[0], pb[3] - pb[1]
                        cx, cy = (tr.box[0] + tr.box[2]) / 2, (tr.box[1] + tr.box[3]) / 2
                        if pb[0] - 0.5 * pw <= cx <= pb[2] + 0.5 * pw and pb[1] - 0.3 * ph <= cy <= pb[3] + 0.3 * ph:
                            fire(tr, t, "yolo:objeto_peligroso", ES[tr.cls][0])
                            break

        # 4) merodeo, objeto abandonado, zonas y estadisticas de flujo
        pers_boxes = [tr.box for _, tr in people]
        speeds, occ = [], 0.0
        for d, tr in assigned:
            cat = _cat(tr.cls)
            if cat == "per" and tr.cls == "person":
                if (tr.t_last - tr.t_first >= LOITER_S and tr.n >= 6 and tr.from_start_w() <= LOITER_MOVE_W
                        and "yolo:merodeo" not in tr.fired):
                    fire(tr, t, "yolo:merodeo", i18n.pick(f"{tr.t_last - tr.t_first:.0f} s en el mismo lugar", f"{tr.t_last - tr.t_first:.0f} s in the same place"))
            elif cat == "obj":
                cx, cy = (tr.box[0] + tr.box[2]) / 2, (tr.box[1] + tr.box[3]) / 2
                for pb in pers_boxes:
                    pw, ph = pb[2] - pb[0], pb[3] - pb[1]
                    if pb[0] - 0.3 * pw <= cx <= pb[2] + 0.3 * pw and pb[1] - 0.2 * ph <= cy <= pb[3] + 0.2 * ph:
                        tr.last_near_t = t
                        break
                idle = t - max(tr.last_near_t, tr.t_first)
                if idle >= ABANDON_S and tr.n >= 4 and tr.from_start_w() <= 1.5:
                    fire(tr, t, "yolo:objeto_abandonado", i18n.pick(f"{ES[tr.cls][0]} sin persona cerca hace {idle:.0f} s", f"{EN_NAMES[tr.cls][0]} with no person nearby for {idle:.0f} s"))
            if cat == "veh":
                occ += (tr.box[2] - tr.box[0]) * (tr.box[3] - tr.box[1])
                v = tr.speed_w()
                if v is not None:
                    speeds.append(v)
            foot = ((tr.box[0] + tr.box[2]) / 2, tr.box[3])
            if cat not in ("obj", "wpn") and zones.get("restricted") and _inside(foot, zones["restricted"]):
                tr.in_restr += 1
                if tr.in_restr >= 2:
                    fire(tr, t, "yolo:intrusion", i18n.pick(f"{ES[tr.cls][0]} dentro de la zona restringida", f"{EN_NAMES[tr.cls][0]} inside the restricted zone"))
            elif cat not in ("obj", "wpn"):
                tr.in_restr = 0
            if (cat == "veh" and zones.get("no_parking") and _inside(foot, zones["no_parking"])
                    and tr.t_last - tr.t_first >= PARK_S):
                dw = tr.disp_w(t - PARK_S, t)
                if dw is not None and dw <= 0.3:
                    fire(tr, t, "yolo:estacionado_prohibido", i18n.pick(f"{ES[tr.cls][0]} detenido {PARK_S:.0f}+ s en zona de no estacionar", f"{EN_NAMES[tr.cls][0]} stopped {PARK_S:.0f}+ s in a no-parking zone"))
        stats.append({"t": t, "veh": sum(1 for _, tr in assigned if tr.cls in VEHICLES),
                      "per": len(people), "occ": min(1.0, occ), "speeds": speeds})

        boxes_out.append([round(t, 2), [[CLASSES.index(d["c"]), round(d["x1"], 3), round(d["y1"], 3),
                                          round(d["x2"], 3), round(d["y2"], 3), tr.id, tr.flag]
                                         for d, tr in assigned]])
    for tg in triggers:
        tg.setdefault("src", "yolo")
    duration = (samples[-1][0] - samples[0][0] + 1.0 / max(YOLO_FPS, 0.1)) if samples else 0.0
    return {"boxes": boxes_out, "triggers": triggers, "flow": _flow(stats, tracks, duration), "zones": zones}


def _nm(cls: str) -> tuple:
    return (EN_NAMES if i18n.en() else ES)[cls]


def _why(why: str) -> str:
    return (WHY_EN if i18n.en() else WHY_ES)[why]


def _plural(cls: str, n: int) -> str:
    one, many = _nm(cls)
    return f"{n} {one if n == 1 else many}"


def hint_for(boxes: list, triggers: list[dict], t: float) -> str:
    """Texto corto para acompanar un frame: conteo de objetos reciente y alertas cercanas (YOLO y calidad de camara)."""
    parts: list[str] = []
    cand = [b for b in (boxes or []) if 0 <= t - b[0] <= 1.6 or 0 <= b[0] - t <= 0.3]
    if cand:
        cur = min(cand, key=lambda b: abs(b[0] - t))
        counts: dict[str, int] = {}
        for item in cur[1]:
            counts[CLASSES[item[0]]] = counts.get(CLASSES[item[0]], 0) + 1
        parts = [_plural(c, n) for c, n in counts.items()] or [i18n.pick("sin objetos", "no objects")]
    near = [tr for tr in triggers if -0.5 <= t - tr["t"] <= 3.5
            or (tr.get("t_end") is not None and tr["t"] <= t <= tr["t_end"] + 0.5)]
    if not parts and not near:
        return ""
    s = i18n.pick("Ayudas automaticas (pueden fallar; confia en la imagen). El detector solo conoce autos, personas, objetos y animales: "
                  "NO reconoce fuego, humo, choques ni heridos, asi que ver pocos objetos no significa que todo este normal",
                  "Automatic aids (they can be wrong; trust the image). The detector only knows cars, people, objects and animals: "
                  "it does NOT recognize fire, smoke, crashes or injuries, so seeing few objects does not mean everything is normal")
    s += ": " + ", ".join(parts) + "." if parts else "."
    if near:
        s += i18n.pick(" Alertas: ", " Alerts: ") + "; ".join(sorted({tr["label"] for tr in near})) + "."
    return s
