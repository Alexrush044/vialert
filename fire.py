"""Deteccion LOCAL de llamas y humo por color y parpadeo (sin modelo de IA, casi sin CPU).

YOLO (COCO) no tiene la clase fuego ni humo, asi que sin esto un incendio solo se enteraria el VLM cuando
le toque un frame de la cuadricula base (cada FRAME_INTERVAL s). Este modulo vigila cada muestra del
pre-escaneo y, si ve algo que parece fuego o humo sostenido, levanta un DISPARADOR: Vialert manda un frame
extra al VLM, que es quien decide si de verdad es FIRE_OR_SMOKE. Un falso positivo solo cuesta un frame.

  fuego: pixeles naranja/amarillo muy brillantes y saturados, que parpadean en el mismo sitio
         (un camion naranja o un cono no parpadean; un vehiculo en marcha se desplaza, una llama no).
  humo : zona gris poco saturada que se mueve y crece de forma sostenida.
"""
import os

import cv2
import numpy as np

ON = os.getenv("FIRE", "1") == "1"
FLAME_MIN_FRAC = float(os.getenv("FIRE_MIN_FRAC", "0.004"))    # fraccion minima de la imagen con color de llama
FLAME_MAX_FRAC = float(os.getenv("FIRE_MAX_FRAC", "0.45"))     # por encima: atardecer, pared naranja, etc.
FLAME_FLICK = float(os.getenv("FIRE_FLICK", "0.12"))           # cambio entre muestras (union/interseccion de la mascara)
FLAME_DRIFT = float(os.getenv("FIRE_DRIFT", "0.06"))           # desplazamiento max. del centro entre muestras (fraccion del ancho)
FLAME_NET = float(os.getenv("FIRE_NET", "0.15"))               # desplazamiento neto max. de todo el tramo: una llama se queda, un vehiculo avanza
FLAME_AREA_CV = float(os.getenv("FIRE_AREA_CV", "0.06"))       # una llama cambia de tamano; un objeto rigido naranja no
FIRE_MIN_S = float(os.getenv("FIRE_MIN_S", "1.5"))             # s sostenidos para considerarlo llama
SMOKE_MIN_FRAC = float(os.getenv("SMOKE_MIN_FRAC", "0.05"))    # fraccion minima de la imagen con gris en movimiento
SMOKE_MAX_FRAC = float(os.getenv("SMOKE_MAX_FRAC", "0.60"))
SMOKE_MIN_S = float(os.getenv("SMOKE_MIN_S", "3"))
W, H = 192, 108

LABEL = {"fire:fuego": "posibles llamas (color y parpadeo)", "fire:humo": "posible humo (zona gris que crece)"}
LABEL_EN = {"fire:fuego": "possible flames (color and flicker)", "fire:humo": "possible smoke (growing gray area)"}


def metrics(view_bgr, prev: dict | None):
    """-> (estado, metricas). `prev` es el estado devuelto en la muestra anterior (o None)."""
    im = cv2.resize(view_bgr, (W, H), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    b, g, r = im[..., 0].astype(np.int16), im[..., 1].astype(np.int16), im[..., 2].astype(np.int16)
    # llama: naranja-amarillo (H 5-35 en OpenCV), muy saturada y muy brillante, con R > G > B
    flame = ((h >= 5) & (h <= 35) & (s >= 110) & (v >= 200) & (r > g) & (g > b) & (r >= 215)).astype(np.uint8)
    frac = float(flame.mean())
    cx = cy = None
    if frac > 0:
        ys, xs = np.nonzero(flame)
        cx, cy = float(xs.mean()) / W, float(ys.mean()) / H
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    flick = drift = 0.0
    smoke = 0.0
    if prev is not None:
        a, bm = prev["flame"], flame
        union = int(((a | bm) > 0).sum())
        if union >= 6:
            flick = float(((a ^ bm) > 0).sum()) / union
        if prev.get("cx") is not None and cx is not None:
            drift = ((cx - prev["cx"]) ** 2 + (cy - prev["cy"]) ** 2) ** 0.5
        moving = cv2.absdiff(gray, prev["gray"]) > 6
        grayish = (s < 55) & (v >= 70) & (v <= 215)
        smoke = float((moving & grayish).mean())
    return {"flame": flame, "cx": cx, "cy": cy, "gray": gray}, {"frac": frac, "flick": flick, "drift": drift, "smoke": smoke,
                                                               "cx": cx, "cy": cy}


def _runs(flags: list[bool]):
    i = 0
    while i < len(flags):
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(flags) and flags[j + 1]:
            j += 1
        yield i, j
        i = j + 1


def analyze(samples: list[tuple[float, dict]], en: bool = False) -> list[dict]:
    """samples: [(t, metricas)] -> eventos {t, t_end, why, label, detail, src:'fire'}. Tolera 1 muestra suelta de hueco."""
    if not ON or len(samples) < 4:
        return []
    step = max(0.05, float(np.median(np.diff([t for t, _ in samples]))))
    fl = [FLAME_MIN_FRAC <= m["frac"] <= FLAME_MAX_FRAC and m["flick"] >= FLAME_FLICK and m["drift"] <= FLAME_DRIFT
          for _, m in samples]
    sm = [SMOKE_MIN_FRAC <= m["smoke"] <= SMOKE_MAX_FRAC for _, m in samples]
    for arr in (fl, sm):                           # rellena huecos de 1 muestra
        for k in range(1, len(arr) - 1):
            if not arr[k] and arr[k - 1] and arr[k + 1]:
                arr[k] = True
    out = []
    for i, j in _runs(fl):
        t0, t1 = samples[i][0], samples[j][0] + step
        if t1 - t0 >= FIRE_MIN_S:
            fracs = np.array([samples[k][1]["frac"] for k in range(i, j + 1)])
            cs = [(samples[k][1]["cx"], samples[k][1]["cy"]) for k in range(i, j + 1) if samples[k][1].get("cx") is not None]
            net = ((cs[-1][0] - cs[0][0]) ** 2 + (cs[-1][1] - cs[0][1]) ** 2) ** 0.5 if len(cs) >= 2 else 0.0
            cv = float(fracs.std() / max(fracs.mean(), 1e-9))
            if net > FLAME_NET or cv < FLAME_AREA_CV:
                continue
            fr = float(fracs.mean()) * 100
            out.append({"t": round(t0, 2), "t_end": round(t1, 2), "why": "fire:fuego",
                        "label": (LABEL_EN if en else LABEL)["fire:fuego"],
                        "detail": (f"{fr:.1f}% of the image" if en else f"{fr:.1f}% de la imagen") + (f" · {t1 - t0:.0f} s"),
                        "src": "fire", "track": None, "cls": None})
    for i, j in _runs(sm):
        t0, t1 = samples[i][0], samples[j][0] + step
        if t1 - t0 >= SMOKE_MIN_S:
            vals = [samples[k][1]["smoke"] for k in range(i, j + 1)]
            half = len(vals) // 2
            if half and np.mean(vals[half:]) >= 0.9 * np.mean(vals[:half]):      # no se apaga: se mantiene o crece
                fr = float(np.mean(vals)) * 100
                out.append({"t": round(t0, 2), "t_end": round(t1, 2), "why": "fire:humo",
                            "label": (LABEL_EN if en else LABEL)["fire:humo"],
                            "detail": (f"{fr:.1f}% of the image moving in gray" if en else f"{fr:.1f}% de la imagen gris en movimiento") + f" · {t1 - t0:.0f} s",
                            "src": "fire", "track": None, "cls": None})
    return sorted(out, key=lambda e: e["t"])
