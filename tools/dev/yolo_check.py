"""Prueba rapida de YOLO en este equipo:  python yolo_check.py [imagen.jpg]
Imprime lo detectado y ms por imagen (relevante para dimensionar el ECS)."""
import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
import sys, time, urllib.request
import cv2
import detector

path = sys.argv[1] if len(sys.argv) > 1 else "bus.jpg"
if len(sys.argv) == 1:
    urllib.request.urlretrieve("https://raw.githubusercontent.com/ultralytics/ultralytics/main/ultralytics/assets/bus.jpg", path)
img = cv2.imread(path)
assert img is not None, f"No se pudo leer {path}"
t = time.time(); detector.get_model(); print(f"modelo {detector.YOLO_WEIGHTS} cargado en {time.time()-t:.1f} s")
detector.detect([img])  # calentamiento
n = 5; t = time.time()
for _ in range(n):
    out = detector.detect([img])[0]
ms = (time.time() - t) * 1000 / n
print(f"{len(out)} objetos:", ", ".join(f"{d['c']} {d['conf']:.2f}" for d in out))
print(f"{ms:.0f} ms por imagen (imgsz={detector.YOLO_IMGSZ}, CPU)")
lote = [img] * detector.YOLO_BATCH; t = time.time(); detector.detect(lote)
print(f"lote de {len(lote)}: {(time.time()-t)*1000/len(lote):.0f} ms por imagen")
