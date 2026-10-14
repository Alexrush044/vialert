"""Calidad de imagen de la camara (local, sin nube): tapada, oscura, desenfocada, movida o congelada.

Se calcula a partir de las muestras del pre-escaneo. Son eventos de ANALITICA: se registran en el informe,
se muestran en el panel y disparan un frame extra para que el modelo confirme (CAMERA_OBSTRUCTED).
"""
import os

import i18n

import cv2
import numpy as np

COVER_STD = float(os.getenv("Q_COVER_STD", "7"))        # contraste (desv. estandar) por debajo = imagen casi uniforme
DARK_MEAN = float(os.getenv("Q_DARK_MEAN", "18"))       # brillo medio por debajo = muy oscuro
BLUR_ABS = float(os.getenv("Q_BLUR_ABS", "10"))         # varianza del Laplaciano absoluta (muy desenfocada)
BLUR_REL = float(os.getenv("Q_BLUR_REL", "0.35"))       # o menos de esta fraccion de la mediana del propio video
JUMP_FRAC = float(os.getenv("Q_JUMP_FRAC", "0.6"))      # fraccion de pixeles que cambian de golpe (camara movida/corte)
FROZEN_DIFF = float(os.getenv("Q_FROZEN_DIFF", "0.02"))  # diferencia media entre muestras por debajo = imagen congelada
MIN_S = {"tapada": 3.0, "oscura": 5.0, "desenfocada": 3.0, "congelada": 5.0, "movida": 0.4}

LABEL = {"cam:tapada": "camara tapada o imagen uniforme", "cam:oscura": "imagen muy oscura",
         "cam:desenfocada": "imagen desenfocada", "cam:movida": "cambio global brusco (camara movida o corte de escena)",
         "cam:congelada": "imagen congelada (posible falla de la camara)"}


LABEL_EN = {"cam:tapada": "blocked camera or uniform image", "cam:oscura": "image too dark",
            "cam:desenfocada": "out-of-focus image", "cam:movida": "sudden global change (camera moved or scene cut)",
            "cam:congelada": "frozen image (possible camera failure)"}


def metrics(view_bgr, prev_gray):
    """-> (gray 160x90, dict de metricas). prev_gray es la muestra anterior (160x90) o None."""
    g = cv2.cvtColor(cv2.resize(view_bgr, (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    g2 = cv2.cvtColor(cv2.resize(view_bgr, (320, 180), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    m = {"mean": float(g.mean()), "std": float(g.std()), "lap": float(cv2.Laplacian(g2, cv2.CV_64F).var())}
    if prev_gray is not None:
        d = cv2.absdiff(g, prev_gray)
        m["diff"] = float(d.mean())
        m["jump"] = float((d > 30).mean())
    else:
        m["diff"], m["jump"] = None, 0.0
    return g, m


def analyze(samples: list[tuple[float, dict]]) -> list[dict]:
    """samples: [(t, metricas)] -> lista de eventos {t, t_end, why, label, detail, src:'cam'}."""
    if len(samples) < 4:
        return []
    laps = np.array([m["lap"] for _, m in samples])
    med_lap = float(np.median(laps))
    flags = {
        "tapada": [m["std"] < COVER_STD for _, m in samples],
        "oscura": [m["mean"] < DARK_MEAN and m["std"] >= COVER_STD for _, m in samples],
        "desenfocada": [(m["lap"] < BLUR_ABS or (med_lap > 40 and m["lap"] < BLUR_REL * med_lap))
                        and m["std"] >= COVER_STD and m["mean"] >= DARK_MEAN for _, m in samples],
        "congelada": [m["diff"] is not None and m["diff"] < FROZEN_DIFF for _, m in samples],
        "movida": [m["jump"] >= JUMP_FRAC for _, m in samples],
    }
    step = max(0.05, float(np.median(np.diff([t for t, _ in samples]))))
    out = []
    for kind, fl in flags.items():
        i = 0
        while i < len(fl):
            if not fl[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(fl) and fl[j + 1]:
                j += 1
            t0, t1 = samples[i][0], samples[j][0] + step
            if t1 - t0 >= MIN_S[kind] - 1e-6 or (kind == "movida" and j > i):
                vals = [samples[k][1] for k in range(i, j + 1)]
                if i18n.en():
                    detail = {"tapada": f"contrast {np.mean([v['std'] for v in vals]):.1f}",
                              "oscura": f"mean brightness {np.mean([v['mean'] for v in vals]):.0f}/255",
                              "desenfocada": f"sharpness {np.mean([v['lap'] for v in vals]):.0f} (video median {med_lap:.0f})",
                              "congelada": "no change between samples",
                              "movida": f"{np.max([v['jump'] for v in vals]) * 100:.0f}% of the image changed at once"}[kind]
                else:
                    detail = {"tapada": f"contraste {np.mean([v['std'] for v in vals]):.1f}",
                              "oscura": f"brillo medio {np.mean([v['mean'] for v in vals]):.0f}/255",
                              "desenfocada": f"nitidez {np.mean([v['lap'] for v in vals]):.0f} (mediana del video {med_lap:.0f})",
                              "congelada": "sin cambios entre muestras",
                              "movida": f"{np.max([v['jump'] for v in vals]) * 100:.0f}% de la imagen cambio de golpe"}[kind]
                out.append({"t": round(t0, 2), "t_end": round(t1, 2), "why": f"cam:{kind}", "label": (LABEL_EN if i18n.en() else LABEL)[f"cam:{kind}"],
                            "detail": detail, "src": "cam", "track": None, "cls": None})
            i = j + 1
    return sorted(out, key=lambda e: e["t"])
